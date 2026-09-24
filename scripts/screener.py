#!/usr/bin/env python3
"""
Aktiescreener för svenska och amerikanska börsen.

Hämtar kursdata via yfinance, beräknar P/E, SMA50/200, RSI14 och
volymavvikelser, och genererar köpkandidater för alla bevakade aktier.

Innehav/säljsignaler hanteras INTE här längre - det sker helt klientsidan
i webbläsaren (docs/index.html) för att innehavsuppgifter aldrig ska
lämna användarens enhet. Servern vet inte vilka aktier någon äger.

Körs antingen manuellt: python scripts/screener.py
eller schemalagt via GitHub Actions (.github/workflows/screener.yml)
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
WATCHLIST_FILE = DATA_DIR / "watchlist.yml"
SCORE_HISTORY_FILE = DATA_DIR / "score_history.json"
OUTPUT_FILE = DOCS_DIR / "results.json"

BUY_SIGNAL_THRESHOLD = 65   # köppoäng för att räknas som "aktiv köpsignal" i dagräkningen
MAX_HISTORY_ENTRIES = 400   # ca 1,5 års vardagskörningar per ticker - för framtida backtesting av poäng vs avkastning

RSI_PERIOD = 14
SMA_SHORTEST = 20  # tidigare varningssignal (SMA20 vs SMA50), snabbare men brusigare än golden/death cross
SMA_SHORT = 50
SMA_LONG = 200
VOLUME_LOOKBACK = 20
HISTORY_PERIOD = "1y"  # needs to cover SMA200 comfortably


def load_watchlist():
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    tickers = []
    for entry in wl.get("stocks", []):
        tickers.append({
            "ticker": entry["ticker"],
            "name": entry.get("name", entry["ticker"]),
            "market": entry.get("market", "?"),
            "sector": entry.get("sector"),
            "country": entry.get("country"),
            "growth_candidate": entry.get("growth_candidate", False),
        })
    return tickers


RISK_FACTORS_FILE = DATA_DIR / "risk_factors.yml"
RISK_FREE_RATES_FILE = DATA_DIR / "risk_free_rates.json"


def load_risk_free_rates():
    """Läser landsspecifika riskfria räntor (10-åriga statsobligationer).
    Returnerar {marknadskod: ränta_i_procent}. Statisk referensfil, inte
    live-hämtad - uppdateras manuellt via Claude när du ber om en avstämning."""
    if not RISK_FREE_RATES_FILE.exists():
        return {}
    with open(RISK_FREE_RATES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("rates") or {}


INVESTTECH_TOP20_FILE = DATA_DIR / "investtech_top20.json"


def load_investtech_top20():
    """Läser Investtechs Topp 20-lista (teknisk analys, Stockholmsbörsen).
    Returnerar {ticker: {rank, score}}. Statisk referensfil, uppdateras
    manuellt via Claude när du ber om en avstämning - inte live-hämtad."""
    if not INVESTTECH_TOP20_FILE.exists():
        return {}
    with open(INVESTTECH_TOP20_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {e["ticker"]: e for e in data.get("entries", [])}


def load_risk_factors():
    """Läser redigerbara geopolitiska/makro-riskfaktorer och summerar vikt per
    sektor OCH per land. Returnerar (sector_weights, sector_factor_names,
    country_weights, country_factor_names)."""
    if not RISK_FACTORS_FILE.exists():
        return {}, {}, {}, {}
    with open(RISK_FACTORS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sector_weights = {}
    sector_factor_names = {}
    country_weights = {}
    country_factor_names = {}
    for factor in data.get("factors") or []:
        for sector, weight in (factor.get("sectors") or {}).items():
            sector_weights[sector] = sector_weights.get(sector, 0) + weight
            sector_factor_names.setdefault(sector, []).append(f"{factor.get('name','?')} ({weight:+d})")
        for country, weight in (factor.get("countries") or {}).items():
            country_weights[country] = country_weights.get(country, 0) + weight
            country_factor_names.setdefault(country, []).append(f"{factor.get('name','?')} ({weight:+d})")
    return sector_weights, sector_factor_names, country_weights, country_factor_names


def load_geopolitics_export(sector_weights, country_weights):
    """Bygger den visningsbara geopolitik/världspolitik-sektionen till
    results.json (MAKRO-panelen): varje faktor med sammanfattning, datum och
    vikter, samt nettovikt per sektor/land. Rent visningsdata - poängsättningen
    använder fortfarande load_risk_factors(). Fel här får aldrig stoppa körningen."""
    try:
        if not RISK_FACTORS_FILE.exists():
            return None
        with open(RISK_FACTORS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        factors = []
        for fac in data.get("factors") or []:
            factors.append({
                "name": str(fac.get("name", "?")),
                "updated": str(fac.get("updated")) if fac.get("updated") is not None else None,
                "summary": fac.get("summary"),
                "sectors": dict(fac.get("sectors") or {}),
                "countries": dict(fac.get("countries") or {}),
                "source_url": fac.get("source_url"),
            })
        return {"factors": factors, "sector_weights": dict(sector_weights), "country_weights": dict(country_weights)}
    except Exception as e:
        print(f"  Geopolitik-export misslyckades (ignoreras): {type(e).__name__}: {e}", file=sys.stderr)
        return None


def load_risk_free_export():
    """Riskfria räntor + metadata för visning i MAKRO-panelen."""
    try:
        if not RISK_FREE_RATES_FILE.exists():
            return None
        with open(RISK_FREE_RATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "rates": data.get("rates") or {},
            "last_updated": data.get("_last_updated"),
            "source": data.get("_source"),
        }
    except Exception as e:
        print(f"  Riskfri-export misslyckades (ignoreras): {type(e).__name__}: {e}", file=sys.stderr)
        return None


def load_score_history():
    if not SCORE_HISTORY_FILE.exists():
        return {}
    with open(SCORE_HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_score_history(history: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(SCORE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, sort_keys=True)


def update_score_history(history: dict, ticker: str, score, today_str: str, price=None):
    """Lägger till dagens köppoäng (och pris, för framtida backtesting) i
    historiken för en ticker och returnerar (dagar_i_rad_med_aktiv_köpsignal,
    poängförändring_sedan_föregående_körning). Kör man screenern flera
    gånger samma dag skrivs den dagens post över istället för att
    dubbleras. (Säljpoäng-historik hanteras numera klientsidan, eftersom
    innehav bara finns i webbläsaren.)"""
    if score is None:
        return None, None

    series = history.setdefault(ticker, {}).setdefault("buy", [])
    series[:] = [e for e in series if e["date"] != today_str]
    entry = {"date": today_str, "score": score}
    if price is not None:
        entry["price"] = price
    series.append(entry)
    series.sort(key=lambda e: e["date"])
    if len(series) > MAX_HISTORY_ENTRIES:
        del series[: -MAX_HISTORY_ENTRIES]

    delta = series[-1]["score"] - series[-2]["score"] if len(series) >= 2 else None

    days = 0
    for entry in reversed(series):
        if entry["score"] >= BUY_SIGNAL_THRESHOLD:
            days += 1
        else:
            break

    return days, delta


def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # Wilder's smoothing
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100)  # if avg_loss is 0, RSI = 100
    return rsi


FMP_API_KEY = os.environ.get("FMP_API_KEY")
FMP_BASE = "https://financialmodelingprep.com/stable"


FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"


def fetch_fred_series(series_id: str, lookback_days: int = 400):
    """Hämtar en tidsserie från FRED (Federal Reserve Economic Data) som CSV
    - helt gratis, ingen API-nyckel krävs, samma källa som riksbanker och
    finansbranschen själva använder. Returnerar en lista av (datum, värde)
    sorterad äldst först, eller tom lista vid nätverksfel."""
    try:
        url = FRED_CSV_URL.format(series_id=series_id)
        with urllib.request.urlopen(url, timeout=15) as resp:
            text = resp.read().decode()
        lines = text.strip().split("\n")
        rows = []
        for line in lines[1:]:  # hoppa över kolumnrubriken
            parts = line.split(",")
            if len(parts) != 2:
                continue
            date_str, val_str = parts
            if val_str in (".", ""):
                continue  # FRED anger saknade observationer (helger etc) som "."
            try:
                rows.append((date_str, float(val_str)))
            except ValueError:
                continue
        return rows[-lookback_days:] if rows else []
    except Exception as e:
        print(f"FRED-hämtning misslyckades för {series_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def compute_macro_indicators():
    """Bygger makropanelen: räntekurvan (10-årig minus 2-årig amerikansk
    statsobligation) och high-yield kreditspread - båda gratis från FRED.
    Lagras separat i results.json (inte per aktie) och används som en bred,
    måttlig försiktighetsvikt i poängmodellerna när räntekurvan varit
    sammanhängande inverterad en tid eller kreditspreadarna är förhöjda."""
    macro = {"as_of": None, "source": "FRED (fred.stlouisfed.org)"}

    yc_rows = fetch_fred_series("T10Y2Y")
    if yc_rows:
        latest_date, latest_val = yc_rows[-1]
        macro["yield_curve_10y2y_pct"] = round(latest_val, 2)
        macro["yield_curve_inverted"] = latest_val < 0
        macro["as_of"] = latest_date
        days_inverted = 0
        for _, val in reversed(yc_rows):
            if val < 0:
                days_inverted += 1
            else:
                break
        macro["yield_curve_inverted_days"] = days_inverted
    else:
        macro["yield_curve_10y2y_pct"] = None
        macro["yield_curve_inverted"] = None
        macro["yield_curve_inverted_days"] = 0

    hy_rows = fetch_fred_series("BAMLH0A0HYM2")
    if hy_rows:
        latest_date_hy, latest_hy = hy_rows[-1]
        macro["credit_spread_hy_pct"] = round(latest_hy, 2)
        if not macro["as_of"]:
            macro["as_of"] = latest_date_hy
        if latest_hy > 8:
            macro["credit_spread_level"] = "kris"
        elif latest_hy > 5:
            macro["credit_spread_level"] = "forhojd"
        else:
            macro["credit_spread_level"] = "normal"
    else:
        macro["credit_spread_hy_pct"] = None
        macro["credit_spread_level"] = None

    return macro


# ---------------------------------------------------------------------------
# Obligationsöversikt + stabilitetspoäng (v8.0)
# ---------------------------------------------------------------------------
BOND_SERIES = [("3M", "DGS3MO"), ("2Y", "DGS2"), ("3Y", "DGS3"), ("5Y", "DGS5"),
               ("7Y", "DGS7"), ("10Y", "DGS10"), ("30Y", "DGS30")]

_RATE_WEEKLY = None  # veckoförändring i amerikansk 10-årsränta (pp), sätts i main()


def _interp(x, pts):
    """Linjär interpolation mellan (x, poäng)-punkter, klippt vid ändarna."""
    if x is None:
        return None
    if x <= pts[0][0]:
        return float(pts[0][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return float(pts[-1][1])


def _at(rows, off):
    """Värde `off` observationer före senaste (0 = senaste)."""
    if not rows or len(rows) <= off:
        return None
    return rows[-1 - off][1]


def _chg(rows, n, off=0):
    a, b = _at(rows, off), _at(rows, off + n)
    return None if a is None or b is None else round(a - b, 2)


def _fetch_move():
    try:
        h = yf.Ticker("^MOVE").history(period="1y", auto_adjust=False).dropna(subset=["Close"])
        return [(str(i.date()), float(v)) for i, v in h["Close"].items()] if len(h) else []
    except Exception as e:
        print(f"MOVE-hämtning misslyckades: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _stability_label(score):
    if score is None:
        return None, "neutral"
    if score >= 70:
        return "Stabilt", "good"
    if score >= 50:
        return "Blandat", "neutral"
    if score >= 30:
        return "Ostabilt", "bad"
    return "Stressat", "bad"


def _category_scores(s, off, moff):
    """Poäng 0-100 per kategori (högre = stabilare för aktier/fonder).
    `off` = dagar bakåt för dagsserier, `moff` = månader bakåt för månadsserier."""
    out = {}
    # 1. Ränterörelse
    y10, y30 = s.get("DGS10"), s.get("DGS30")
    c10, c30 = _chg(y10, 21, off), _chg(y30, 21, off)
    chs = [c for c in (c10, c30) if c is not None]
    if chs:
        sc = 70 - 60 * (sum(chs) / len(chs))
        if _at(y30, off) is not None and _at(y30, off) >= 5.0:
            sc -= 15
        if _at(y10, off) is not None and _at(y10, off) >= 5.0:
            sc -= 5
        out["rates"] = max(0, min(100, sc))
    # 2. Räntekurva
    parts = []
    t210, t103 = _at(s.get("T10Y2Y"), off), _at(s.get("T10Y3M"), off)
    for t in (t210, t103):
        if t is not None:
            parts.append(max(0, min(100, 55 + 30 * t)))
    if parts:
        out["curve"] = sum(parts) / len(parts)
    # 3. Kredit
    parts = []
    hy, ig = _at(s.get("BAMLH0A0HYM2"), off), _at(s.get("BAMLC0A0CM"), off)
    if hy is not None:
        h = _interp(hy, [(2, 100), (3.5, 90), (5, 65), (8, 20), (11, 5)])
        hyc = _chg(s.get("BAMLH0A0HYM2"), 21, off)
        if hyc is not None and hyc > 0.5:
            h -= 10
        parts.append(max(0, h))
    if ig is not None:
        parts.append(_interp(ig, [(0.6, 100), (1.0, 90), (1.5, 65), (2.5, 20), (3.5, 5)]))
    if parts:
        out["credit"] = sum(parts) / len(parts)
    # 4. Realränta & inflationsförväntan
    parts = []
    real, be = _at(s.get("DFII10"), off), _at(s.get("T10YIE"), off)
    if real is not None:
        parts.append(_interp(real, [(0, 100), (1, 85), (2, 65), (2.5, 50), (3.5, 20)]))
    if be is not None:
        parts.append(_interp(be, [(1.0, 40), (1.75, 85), (2.25, 100), (2.75, 85), (3.25, 55), (4, 20)]))
    if parts:
        out["real"] = sum(parts) / len(parts)
    # 5. Obligationsvolatilitet
    mv = _at(s.get("MOVE"), off)
    if mv is not None:
        out["vol"] = _interp(mv, [(60, 100), (80, 90), (100, 70), (130, 35), (180, 5)])
    # 6. Europa & Norden (månadsdata från OECD via FRED)
    chs = [_chg(s.get(k), 1, moff) for k in ("IRLTLT01DEM156N", "IRLTLT01SEM156N")]
    chs = [c for c in chs if c is not None]
    if chs:
        out["europe"] = _interp(sum(chs) / len(chs), [(-0.4, 100), (0, 75), (0.3, 50), (0.7, 20)])
    return out


CATEGORY_META = [
    ("rates", "Ränterörelse", 1.5), ("curve", "Räntekurva", 1.0), ("credit", "Kredit", 1.5),
    ("real", "Realränta & inflation", 1.0), ("vol", "Obligationsvolatilitet", 1.0),
    ("europe", "Europa & Norden", 0.5),
]


def _overall(cats):
    num = sum(cats[k] * w for k, _, w in CATEGORY_META if k in cats)
    den = sum(w for k, _, w in CATEGORY_META if k in cats)
    return round(num / den, 1) if den else None


def _fmt(v, d=2):
    return "–" if v is None else f"{v:.{d}f}".replace(".", ",")


def _sg(v, d=2):
    return "–" if v is None else f"{v:+.{d}f}".replace(".", ",")


def compute_bond_dashboard():
    """Ränteöversikt (3 mån-30 år), kurvform, kredit, realränta, MOVE, Europa
    samt en stabilitetspoäng per kategori + totalt. Endast visning - påverkar
    inte aktiernas poäng. Returnerar (dashboard, dgs10_rader)."""
    s = {}
    for _, sid in BOND_SERIES:
        s[sid] = fetch_fred_series(sid, lookback_days=500)
    for sid in ("T10Y2Y", "T10Y3M", "BAMLH0A0HYM2", "BAMLC0A0CM", "DFII10", "T10YIE"):
        s[sid] = fetch_fred_series(sid, lookback_days=500)
    for sid in ("IRLTLT01DEM156N", "IRLTLT01SEM156N"):
        s[sid] = fetch_fred_series(sid, lookback_days=60)
    s["MOVE"] = _fetch_move()
    if not s.get("DGS10"):
        return None, []

    yields = []
    for label, sid in BOND_SERIES:
        rows = s[sid]
        if not rows:
            continue
        v, c1w, c1m = rows[-1][1], _chg(rows, 5), _chg(rows, 21)
        ref = c1m if c1m is not None else 0
        status = "bad" if ref > 0.25 else ("good" if ref < -0.25 else "neutral")
        yields.append({"label": label, "value": round(v, 2), "chg_1w": c1w, "chg_1m": c1m,
                       "date": rows[-1][0], "status": status,
                       "alert": bool(label == "30Y" and v >= 5.0)})

    cats_now = _category_scores(s, 0, 0)
    cats_prev = _category_scores(s, 21, 1)
    total, prev_total = _overall(cats_now), _overall(cats_prev)

    y10, y30, y2 = _at(s["DGS10"], 0), _at(s.get("DGS30"), 0), _at(s.get("DGS2"), 0)
    hy, ig = _at(s["BAMLH0A0HYM2"], 0), _at(s["BAMLC0A0CM"], 0)
    real, be, mv = _at(s["DFII10"], 0), _at(s["T10YIE"], 0), _at(s["MOVE"], 0)
    de, se = s["IRLTLT01DEM156N"], s["IRLTLT01SEM156N"]
    details = {
        "rates": f"10 år {_fmt(y10)} % ({_sg(_chg(s['DGS10'], 21))} pp/mån), 30 år {_fmt(y30)} % ({_sg(_chg(s['DGS30'], 21))} pp/mån)"
                 + (" – över 5 %-nivån" if y30 is not None and y30 >= 5 else ""),
        "curve": f"10 år−2 år {_fmt(_at(s['T10Y2Y'], 0))} pp, 10 år−3 mån {_fmt(_at(s['T10Y3M'], 0))} pp",
        "credit": f"High-yield {_fmt(hy)} pp, investment grade {_fmt(ig)} pp",
        "real": f"Realränta 10 år {_fmt(real)} %, inflationsförväntan {_fmt(be)} %",
        "vol": f"MOVE-index {_fmt(mv, 1)}",
        "europe": f"Tyskland 10 år {_fmt(_at(de, 0))} %, Sverige 10 år {_fmt(_at(se, 0))} % (månadsdata, {de[-1][0][:7] if de else '–'})",
    }
    categories = []
    for k, name, w in CATEGORY_META:
        if k not in cats_now:
            continue
        label, status = _stability_label(cats_now[k])
        prev = cats_prev.get(k)
        categories.append({"key": k, "name": name, "score": round(cats_now[k]), "prev_score": None if prev is None else round(prev),
                           "label": label, "status": status, "weight": w, "detail": details[k]})
    label, status = _stability_label(total)
    delta = None if total is None or prev_total is None else round(total - prev_total, 1)
    trend = None if delta is None else ("stabilare" if delta >= 3 else ("mindre_stabilt" if delta <= -3 else "oförändrat"))

    extras = {
        "real_yield_10y": real, "breakeven_10y": be, "hy_spread": hy, "ig_spread": ig, "move": mv,
        "spread_10y2y": _at(s["T10Y2Y"], 0), "spread_10y3m": _at(s["T10Y3M"], 0),
        "de_10y": _at(de, 0), "se_10y": _at(se, 0), "eu_as_of": de[-1][0] if de else None,
        "real_yield_chg_1m": _chg(s["DFII10"], 21), "breakeven_chg_1m": _chg(s["T10YIE"], 21),
        "hy_chg_1m": _chg(s["BAMLH0A0HYM2"], 21), "ig_chg_1m": _chg(s["BAMLC0A0CM"], 21),
        "move_chg_1m": None if not s["MOVE"] or len(s["MOVE"]) < 22 else round(s["MOVE"][-1][1] - s["MOVE"][-22][1], 1),
    }
    return {"as_of": s["DGS10"][-1][0], "yields": yields, "extras": extras, "categories": categories,
            "stability": {"score": total, "prev_score": prev_total, "delta": delta, "trend": trend,
                          "label": label, "status": status}}, s["DGS10"]


def set_rate_series(dgs10_rows):
    global _RATE_WEEKLY
    if not dgs10_rows:
        return
    ser = pd.Series({pd.Timestamp(d): v for d, v in dgs10_rows}).sort_index()
    _RATE_WEEKLY = ser.resample("W-FRI").last().diff().dropna()


def compute_rate_beta(close):
    """Aktiens veckoavkastning (%) per +1 pp i amerikansk 10-årsränta (1 år
    veckodata - grov uppskattning). Returnerar (beta, r2) eller (None, None)."""
    if _RATE_WEEKLY is None:
        return None, None
    try:
        c = close.copy()
        if getattr(c.index, "tz", None) is not None:
            c.index = c.index.tz_localize(None)
        w = (c.resample("W-FRI").last().pct_change() * 100).rename("r")
        df = pd.concat([w, _RATE_WEEKLY.rename("x")], axis=1, join="inner").dropna()
        if len(df) < 26 or df["x"].var() <= 0:
            return None, None
        beta = df["r"].cov(df["x"]) / df["x"].var()
        r2 = df["r"].corr(df["x"]) ** 2
        return round(float(beta), 2), round(float(r2), 3)
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Värdering mot egen historik, efter bolagets fas (v8.2)
# ---------------------------------------------------------------------------
CYCLICAL_SECTORS = {"Energy", "Materials", "Semiconductors"}
VAL_CHEAP, VAL_EXPENSIVE = 0.85, 1.15   # nu / historisk median


def _fin_row(df, *names):
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            s = df.loc[n].dropna()
            if len(s):
                return s.sort_index()
    return None


_FX_CACHE = {}


def _fx_factor(fin_ccy, trade_ccy):
    """Faktor för att räkna om rapportvaluta till handelsvaluta (aktuell kurs)."""
    if not fin_ccy or not trade_ccy:
        return 1.0
    pence = trade_ccy == "GBp"
    tc = "GBP" if pence else trade_ccy
    f = 1.0
    if fin_ccy.upper() != tc.upper():
        key = f"{fin_ccy.upper()}{tc.upper()}=X"
        if key not in _FX_CACHE:
            try:
                h = yf.Ticker(key).history(period="5d")["Close"].dropna()
                _FX_CACHE[key] = float(h.iloc[-1]) if len(h) else None
            except Exception:
                _FX_CACHE[key] = None
        if _FX_CACHE[key] is None:
            return None
        f = _FX_CACHE[key]
    return f * (100 if pence else 1)


def compute_valuation(tk, info, pe, forward_pe, revenue_growth_yoy_pct, dividend_yield_pct, market_cap):
    """Klassar bolaget i fas och jämför dagens P/E (P/S för förlustbolag) med
    bolagets egen median de senaste ~4 åren. Vinst/försäljning per aktie
    interpoleras mellan bokslut så att historiken motsvarar rullande 12 mån,
    och senaste punkten är de fyra senaste kvartalen. Returnerar dict eller None."""
    try:
        inc, qinc, cf = tk.income_stmt, tk.quarterly_income_stmt, tk.cash_flow
    except Exception:
        return None
    ni = _fin_row(inc, "Net Income Common Stockholders", "Net Income")
    rev = _fin_row(inc, "Total Revenue", "Operating Revenue")
    sh = _fin_row(inc, "Diluted Average Shares", "Basic Average Shares")
    if rev is None or sh is None or len(rev) < 2:
        return None
    fx = _fx_factor(info.get("financialCurrency"), info.get("currency"))
    if fx is None:
        return None

    growing = revenue_growth_yoy_pct > 0 if revenue_growth_yoy_pct is not None else float(rev.iloc[-1]) > float(rev.iloc[-2])
    profitable = (isinstance(pe, (int, float)) and pe > 0) or (ni is not None and float(ni.iloc[-1]) > 0)
    buyback = _fin_row(cf, "Repurchase Of Capital Stock", "Common Stock Payments")
    bb_amt = abs(float(buyback.iloc[-1])) if buyback is not None else 0
    returns_capital = bool((dividend_yield_pct or 0) > 0.3 or (market_cap and bb_amt * fx > 0.005 * market_cap))
    if not profitable and growing:
        phase, phase_name, metric = 2, "Tillväxtfas (ej lönsam än)", "ps"
    elif not profitable:
        phase, phase_name, metric = 6, "Förlust och krympande försäljning", "ps"
    elif not growing:
        phase, phase_name, metric = 5, "Mogen fas (krympande försäljning)", "pe"
    elif returns_capital:
        phase, phase_name, metric = 4, "Kapitalåterföringsfas", "pe"
    else:
        phase, phase_name, metric = 3, "Lönsam tillväxtfas", "pe"

    base = ni if metric == "pe" else rev
    if base is None:
        return None
    pts = {}
    for dt in base.index:
        if dt in sh.index and sh[dt] and sh[dt] > 0:
            pts[pd.Timestamp(dt)] = float(base[dt]) / float(sh[dt]) * fx
    # Rullande 12 mån från kvartalsdata som senaste punkt
    qb = _fin_row(qinc, "Net Income Common Stockholders", "Net Income") if metric == "pe" else _fin_row(qinc, "Total Revenue", "Operating Revenue")
    qs = _fin_row(qinc, "Diluted Average Shares", "Basic Average Shares")
    if qb is not None and qs is not None and len(qb) >= 4:
        last4 = qb.iloc[-4:]
        if (last4.index[-1] - last4.index[0]).days < 330:
            pts[pd.Timestamp(last4.index[-1])] = float(last4.sum()) / float(qs.iloc[-1]) * fx
    if len(pts) < 2:
        return None
    ser = pd.Series(pts).sort_index()

    try:
        mh = tk.history(period="5y", interval="1mo", auto_adjust=False)["Close"].dropna()
        if getattr(mh.index, "tz", None) is not None:
            mh.index = mh.index.tz_localize(None)
    except Exception:
        return None
    if len(mh) < 24:
        return None
    lag = pd.Timedelta(days=45)
    xs = [pd.Timestamp(d).timestamp() for d in ser.index]
    import numpy as _np
    mults = []
    for t, px in mh.items():
        tt = t - lag
        if tt < ser.index[0]:
            continue
        v = float(_np.interp(pd.Timestamp(tt).timestamp(), xs, ser.values))
        if v <= 0:
            continue
        m = px / v
        if 0 < m < (150 if metric == "pe" else 60):
            mults.append(m)
    if len(mults) < 18:
        return None
    hist_own = float(pd.Series(mults).median())
    cur_v = float(ser.iloc[-1])
    if cur_v <= 0:
        return None
    cur_own = float(mh.iloc[-1]) / cur_v
    ratio = cur_own / hist_own
    yahoo_now = pe if metric == "pe" else info.get("priceToSalesTrailing12Months")
    now_disp = float(yahoo_now) if isinstance(yahoo_now, (int, float)) and yahoo_now > 0 else cur_own
    hist_disp = now_disp / ratio
    verdict = "cheap" if ratio < VAL_CHEAP else ("expensive" if ratio > VAL_EXPENSIVE else "fair")
    flags = []
    if metric == "pe" and isinstance(forward_pe, (int, float)) and isinstance(pe, (int, float)) and pe > 0 and forward_pe > pe * 1.10:
        flags.append("earnings_falling")
    return {
        "phase": phase, "phase_name": phase_name, "metric": "P/E" if metric == "pe" else "P/S",
        "current": round(now_disp, 1), "hist_median": round(hist_disp, 1),
        "diff_pct": round((ratio - 1) * 100, 1), "verdict": verdict, "flags": flags,
        "years": round(len(mults) / 12, 1),
        "checks": {"growing": bool(growing), "profitable": bool(profitable), "returns_capital": returns_capital},
    }


def valuation_weight(val, sector):
    """Måttlig, graderad poängpåverkan efter avvikelse mot egen median:
    15-30 % -> +/-3, över 30 % -> +/-6. Halveras för cykliska sektorer;
    ingen bonus om vinsten väntas falla. Returnerar (vikt, text)."""
    if not val or val.get("verdict") == "fair":
        return 0, None
    diff = abs(val.get("diff_pct") or 0)
    w = 6 if diff > 30 else 3
    if val["verdict"] == "expensive":
        w = -w
    if w > 0 and "earnings_falling" in (val.get("flags") or []):
        w = 0
    if sector in CYCLICAL_SECTORS:
        w = int(w / 2)
    if not w:
        return 0, None
    word = "billig" if w > 0 else "dyr"
    return w, (f"Värdering: {word} mot egen historik ({val['metric']} {val['current']} mot median "
               f"{val['hist_median']}, {val['diff_pct']:+.0f} %)")


def _fmp_get(endpoint: str, symbol: str):
    """Enkelt GET-anrop mot FMP:s stable-API. Returnerar None vid fel av
    något slag (saknad nyckel, kvot slut, premium-låst, nätverksfel) -
    ska ALDRIG krascha resten av körningen. Bara amerikanska aktier har
    täckning på gratisnivån."""
    if not FMP_API_KEY:
        return None
    url = f"{FMP_BASE}/{endpoint}?symbol={symbol}&apikey={FMP_API_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception:
        return None


def fetch_fmp_fundamentals(ticker: str):
    """Hämtar kapitalintensitet/kassaflödesnyckeltal från FMP istället för
    yfinance, för amerikanska aktier (bättre standardiserade fältnamn än
    yfinances kassaflödesrapport). Returnerar None om något saknas - då
    behåller analyze_ticker() sina yfinance-värden istället (reserv)."""
    cf = _fmp_get("cash-flow-statement", ticker)
    inc = _fmp_get("income-statement", ticker)
    if not cf or not inc:
        return None

    try:
        capex = cf.get("capitalExpenditure")
        da = cf.get("depreciationAndAmortization")
        fcf = cf.get("freeCashFlow")
        sbc = cf.get("stockBasedCompensation")
        revenue = inc.get("revenue")

        capex_to_da = abs(capex) / abs(da) if capex is not None and da else None
        fcf_margin_pct = (fcf / revenue) * 100 if fcf is not None and revenue else None
        sbc_to_revenue_pct = (abs(sbc) / revenue) * 100 if sbc is not None and revenue else None

        if capex_to_da is None and fcf_margin_pct is None and sbc_to_revenue_pct is None:
            return None

        return {
            "capex_to_da": round(capex_to_da, 2) if capex_to_da is not None else None,
            "fcf_margin_pct": round(fcf_margin_pct, 1) if fcf_margin_pct is not None else None,
            "sbc_to_revenue_pct": round(sbc_to_revenue_pct, 1) if sbc_to_revenue_pct is not None else None,
        }
    except Exception:
        return None


def analyze_ticker(ticker: str):
    tk = yf.Ticker(ticker)
    hist = tk.history(period=HISTORY_PERIOD, auto_adjust=True)
    if hist.empty:
        return None

    # Yahoo Finance lägger ibland till en ofärdig "dagens datum"-rad utan
    # stängningskurs för marknader som inte hunnit öppna/stänga än när
    # workflowen körs (t.ex. asiatiska börser, som körs mitt i natten deras
    # tid). Ta bort sådana rader innan vi räknar på något - annars blir
    # priset NaN, vilket i sin tur gör hela results.json ogiltig JSON.
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 30:
        return None

    close = hist["Close"]
    volume = hist["Volume"]

    # Prisserier för minigrafer i dashboarden: ~3 månader (63 handelsdagar)
    # och hela det redan hämtade året. Ingen extra API-kostnad - vi har
    # redan 1 års data för de tekniska indikatorerna.
    price_history_3m = [round(float(v), 4) for v in close.tail(63)]
    price_history_1y = [round(float(v), 4) for v in close]

    sma20 = close.rolling(SMA_SHORTEST).mean()
    sma50 = close.rolling(SMA_SHORT).mean()
    sma200 = close.rolling(SMA_LONG).mean() if len(close) >= SMA_LONG else pd.Series([None] * len(close))
    rsi = compute_rsi(close)
    avg_volume = volume.rolling(VOLUME_LOOKBACK).mean()

    # Historisk volatilitet: annualiserad std-avvikelse på dagliga
    # avkastningar (senaste ~60 handelsdagarna), i procent. Högre = större
    # och mer oregelbundna kurssvängningar.
    daily_returns = close.pct_change().dropna()
    volatility_pct = None
    if len(daily_returns) >= 20:
        window = daily_returns.tail(60)
        volatility_pct = float(window.std() * (252 ** 0.5) * 100)

    last_close = float(close.iloc[-1])
    last_sma20 = float(sma20.iloc[-1]) if not pd.isna(sma20.iloc[-1]) else None
    last_sma50 = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else None
    last_sma200 = float(sma200.iloc[-1]) if len(sma200) and not pd.isna(sma200.iloc[-1]) else None
    last_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else None
    last_volume = float(volume.iloc[-1])
    last_avg_volume = float(avg_volume.iloc[-1]) if not pd.isna(avg_volume.iloc[-1]) else None
    volume_ratio = (last_volume / last_avg_volume) if last_avg_volume else None

    # golden/death cross detection: did SMA50 cross SMA200 in the last 5 sessions?
    cross_signal = None
    if last_sma200 is not None and len(sma50.dropna()) > 5 and len(sma200.dropna()) > 5:
        diff_now = sma50.iloc[-1] - sma200.iloc[-1]
        diff_prev = sma50.iloc[-6] - sma200.iloc[-6]
        if diff_prev < 0 and diff_now > 0:
            cross_signal = "golden_cross"
        elif diff_prev > 0 and diff_now < 0:
            cross_signal = "death_cross"

    # Samma logik men för SMA20 vs SMA50 - en snabbare, tidigare varning
    # (mer brusig, men reagerar innan den tröga golden/death cross gör det).
    cross_signal_20_50 = None
    if last_sma50 is not None and len(sma20.dropna()) > 5 and len(sma50.dropna()) > 5:
        diff_now_20_50 = sma20.iloc[-1] - sma50.iloc[-1]
        diff_prev_20_50 = sma20.iloc[-6] - sma50.iloc[-6]
        if diff_prev_20_50 < 0 and diff_now_20_50 > 0:
            cross_signal_20_50 = "golden_cross"
        elif diff_prev_20_50 > 0 and diff_now_20_50 < 0:
            cross_signal_20_50 = "death_cross"

    info = {}
    try:
        info = tk.get_info()
    except Exception:
        pass
    pe = info.get("trailingPE")
    forward_pe = info.get("forwardPE")
    peg = info.get("trailingPegRatio")
    if peg is None:
        peg = info.get("pegRatio")

    # Jämför forward P/E (baserat på förväntad framtida vinst) mot trailing
    # P/E (baserat på senast rapporterade vinst). Lägre forward P/E än
    # trailing = vinsttillväxt väntas. Högre = vinstnedgång väntas.
    forward_pe_trend_pct = None
    if isinstance(pe, (int, float)) and isinstance(forward_pe, (int, float)) and pe > 0:
        forward_pe_trend_pct = (forward_pe - pe) / pe * 100

    currency = info.get("currency")
    long_name = info.get("longName") or info.get("shortName")

    # Fler nyckeltal
    pb = info.get("priceToBook")
    div_yield_raw = info.get("dividendYield")
    dividend_yield_pct = None
    if isinstance(div_yield_raw, (int, float)):
        # yfinance växlar ibland mellan andel (0.024) och procent (2.4) beroende på version
        dividend_yield_pct = div_yield_raw * 100 if div_yield_raw < 1 else div_yield_raw
    debt_to_equity_raw = info.get("debtToEquity")
    debt_to_equity = None
    if isinstance(debt_to_equity_raw, (int, float)):
        # yfinance ger normalt debtToEquity som procent (t.ex. 45.2 = 45.2%)
        debt_to_equity = debt_to_equity_raw

    # Analytikerkonsensus (betrodda tredjepartsinstanser via Yahoo Finance-aggregering)
    recommendation_key = info.get("recommendationKey")  # t.ex. 'strong_buy','buy','hold','sell','strong_sell','none'
    num_analysts = info.get("numberOfAnalystOpinions")
    target_mean = info.get("targetMeanPrice")

    # Yahoo Finance anger brittiska aktier i pence (GBp), inte pund (GBP) -
    # t.ex. visas BP som "568.4 GBp" istället för korrekta 5.684 GBP. Lätt
    # att missläsa "GBp" som "GBP" och tro att priset är 100x för högt.
    # Konverterar alla prisrelaterade värden en gång här så allt nedströms
    # (SMA, prisgrafer, kursmål) blir konsekvent i riktiga pund.
    if currency == "GBp":
        last_close /= 100
        if last_sma20 is not None:
            last_sma20 /= 100
        if last_sma50 is not None:
            last_sma50 /= 100
        if last_sma200 is not None:
            last_sma200 /= 100
        price_history_3m = [round(v / 100, 4) for v in price_history_3m]
        price_history_1y = [round(v / 100, 4) for v in price_history_1y]
        if isinstance(target_mean, (int, float)):
            target_mean = target_mean / 100
        currency = "GBP"

    # Full uppdelning av hur många analytiker som satt varje enskilt betyg
    # (inte bara den sammanfattande konsensusetiketten ovan).
    recommendation_breakdown = None
    try:
        rec_df = tk.get_recommendations()
        if rec_df is not None and not rec_df.empty:
            row = rec_df.iloc[0]  # "0m" - senaste perioden, alltid först
            breakdown = {
                "strong_buy": int(row.get("strongBuy", 0) or 0),
                "buy": int(row.get("buy", 0) or 0),
                "hold": int(row.get("hold", 0) or 0),
                "sell": int(row.get("sell", 0) or 0),
                "strong_sell": int(row.get("strongSell", 0) or 0),
            }
            if sum(breakdown.values()) > 0:
                recommendation_breakdown = breakdown
    except Exception:
        pass

    analyst_upside = None
    if isinstance(target_mean, (int, float)) and last_close:
        analyst_upside = (target_mean - last_close) / last_close * 100

    market_cap = info.get("marketCap")
    beta = info.get("beta")
    avg_dollar_volume = (last_avg_volume * last_close) if (last_avg_volume and last_close) else None

    # Kapitalintensitet, kassaflödesmarginal, kvalitet på vinsten och ROIC.
    # Hämtas från kassaflödes-, resultat- och balansräkning (separata
    # yfinance-anrop). Varje källa hämtas OBEROENDE av de andra - om t.ex.
    # kassaflödesdata saknas för ett bolag ska det inte hindra ROIC (som
    # bara behöver resultat- och balansräkning) från att ändå beräknas.
    capex_to_da = None
    fcf_margin_pct = None
    sbc_to_revenue_pct = None
    roic_pct = None
    roic_avg_pct = None
    roic_min_pct = None
    roic_years_count = 0

    cashflow = income = balance = None
    try:
        cashflow = tk.get_cashflow()
    except Exception as e:
        print(f"  Kassaflödesdata saknas/fel för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        income = tk.get_income_stmt()
    except Exception as e:
        print(f"  Resultaträkning saknas/fel för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
    try:
        balance = tk.get_balance_sheet()
    except Exception as e:
        print(f"  Balansräkning saknas/fel för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)

    def _latest(df, row_names):
        if df is None or df.empty:
            return None
        col = df.columns[0]
        for name in row_names:
            if name in df.index:
                val = df.loc[name, col]
                if pd.notna(val):
                    return float(val)
        return None

    try:
        capex = _latest(cashflow, ["Capital Expenditure", "CapitalExpenditure", "Purchase Of PPE"])
        da = _latest(cashflow, ["Depreciation And Amortization", "Depreciation Amortization Depletion", "Depreciation"])
        fcf = _latest(cashflow, ["Free Cash Flow", "FreeCashFlow"])
        sbc = _latest(cashflow, ["Stock Based Compensation", "StockBasedCompensation"])
        revenue = _latest(income, ["Total Revenue", "TotalRevenue", "Operating Revenue", "Revenue"])

        if capex is not None and da:
            capex_to_da = abs(capex) / abs(da)
        if fcf is not None and revenue:
            fcf_margin_pct = (fcf / revenue) * 100
        if sbc is not None and revenue:
            sbc_to_revenue_pct = (abs(sbc) / revenue) * 100
    except Exception as e:
        print(f"  Kassaflödesnyckeltal misslyckades för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        # ROIC (avkastning på investerat kapital) per redovisat räkenskapsår
        # = NOPAT / investerat kapital. NOPAT = rörelseresultat (EBIT) x
        # (1 - effektiv skattesats, begränsad till 0-35%, 21% som reserv om
        # den inte går att räkna fram). Investerat kapital tas direkt från
        # balansräkningens egen rad om den finns, annars eget kapital +
        # räntebärande skulder - kassa.
        #
        # yfinance ger normalt bara ca 3-4 årsbokslut, inte tio - vi kräver
        # minst 3 giltiga år innan något ROIC-värde alls sparas, och är
        # ärliga om den kortare tidshorisonten i gränssnittet. Varje års
        # beräkning hoppas över (räknas inte med) om underlag saknas, om
        # investerat kapital blir noll/negativt, eller om resultatet blir
        # orimligt högt (>100%) - sånt tyder på ett engångsutslag, inte en
        # verklig, hållbar ROIC-nivå.
        def _val(df, row_names, col):
            for name in row_names:
                if name in df.index:
                    v = df.loc[name, col]
                    if pd.notna(v):
                        return float(v)
            return None

        roic_by_year = []
        if income is not None and not income.empty and balance is not None and not balance.empty:
            for col in income.columns:
                if col not in balance.columns:
                    continue
                op_inc = _val(income, ["Operating Income", "OperatingIncome"], col)
                if op_inc is None:
                    continue
                tax_prov = _val(income, ["Tax Provision", "TaxProvision"], col)
                pretax = _val(income, ["Pretax Income", "PretaxIncome"], col)

                invested = _val(balance, ["Invested Capital", "InvestedCapital"], col)
                if invested is None:
                    debt = _val(balance, ["Total Debt", "TotalDebt"], col)
                    equity = _val(balance, ["Stockholders Equity", "StockholdersEquity", "Total Equity Gross Minority Interest", "TotalEquityGrossMinorityInterest"], col)
                    cash = _val(balance, ["Cash And Cash Equivalents", "CashAndCashEquivalents", "Cash Cash Equivalents And Short Term Investments", "CashCashEquivalentsAndShortTermInvestments"], col) or 0
                    if debt is not None and equity is not None:
                        invested = debt + equity - cash
                if invested is None or invested <= 0:
                    continue

                tax_rate = 0.21
                if tax_prov is not None and pretax and pretax > 0:
                    tax_rate = max(0.0, min(0.35, tax_prov / pretax))
                nopat = op_inc * (1 - tax_rate)
                year_roic = (nopat / invested) * 100
                if year_roic > 100:
                    continue
                roic_by_year.append(year_roic)  # income.columns är nyast->äldst

        roic_years_count = len(roic_by_year)
        if roic_years_count >= 3:
            roic_pct = roic_by_year[0]
            roic_avg_pct = sum(roic_by_year) / roic_years_count
            roic_min_pct = min(roic_by_year)
    except Exception as e:
        print(f"  ROIC-beräkning misslyckades för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)
        roic_pct = roic_avg_pct = roic_min_pct = None
        roic_years_count = 0

    # Flerkvartals tillväxt- och marginaltrend. Jämför senaste kvartalet mot
    # samma kvartal föregående år (undviker säsongseffekter). Kräver minst
    # 5 kvartal historik - saknas ofta för mindre/utländska bolag, då
    # degraderar fälten bara till None.
    revenue_growth_yoy_pct = None
    operating_margin_trend_pp = None
    try:
        q_income = tk.get_income_stmt(freq="quarterly")
        if q_income is not None and not q_income.empty and len(q_income.columns) >= 5:
            cols = list(q_income.columns)  # nyast först
            latest_col, year_ago_col = cols[0], cols[4]

            def _row(name):
                return name if name in q_income.index else None

            rev_row = _row("Total Revenue") or _row("TotalRevenue")
            op_row = _row("Operating Income") or _row("OperatingIncome") or _row("Operating Revenue") or _row("OperatingRevenue")

            if rev_row:
                rev_latest = q_income.loc[rev_row, latest_col]
                rev_year_ago = q_income.loc[rev_row, year_ago_col]
                if pd.notna(rev_latest) and pd.notna(rev_year_ago) and rev_year_ago:
                    revenue_growth_yoy_pct = (float(rev_latest) - float(rev_year_ago)) / abs(float(rev_year_ago)) * 100

                if op_row:
                    op_latest = q_income.loc[op_row, latest_col]
                    op_year_ago = q_income.loc[op_row, year_ago_col]
                    if pd.notna(op_latest) and pd.notna(op_year_ago) and rev_latest and rev_year_ago:
                        margin_latest = float(op_latest) / float(rev_latest) * 100
                        margin_year_ago = float(op_year_ago) / float(rev_year_ago) * 100
                        operating_margin_trend_pp = margin_latest - margin_year_ago
    except Exception as e:
        print(f"  Kvartalstrend saknas/fel för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)

    valuation = None
    try:
        valuation = compute_valuation(tk, info, pe, forward_pe, revenue_growth_yoy_pct, dividend_yield_pct, market_cap)
    except Exception as e:
        print(f"  Värdering saknas/fel för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)

    return {
        "ticker": ticker,
        "name": long_name,
        "currency": currency,
        "price": round(last_close, 2),
        "pe": round(pe, 2) if isinstance(pe, (int, float)) else None,
        "forward_pe": round(forward_pe, 2) if isinstance(forward_pe, (int, float)) else None,
        "forward_pe_trend_pct": round(forward_pe_trend_pct, 1) if forward_pe_trend_pct is not None else None,
        "peg_ratio": round(peg, 2) if isinstance(peg, (int, float)) else None,
        "revenue_growth_yoy_pct": round(revenue_growth_yoy_pct, 1) if revenue_growth_yoy_pct is not None else None,
        "operating_margin_trend_pp": round(operating_margin_trend_pp, 1) if operating_margin_trend_pp is not None else None,
        "sma20": round(last_sma20, 2) if last_sma20 else None,
        "sma50": round(last_sma50, 2) if last_sma50 else None,
        "cross_signal_20_50": cross_signal_20_50,
        "sma200": round(last_sma200, 2) if last_sma200 else None,
        "rate_beta": compute_rate_beta(close)[0],
        "rate_beta_r2": compute_rate_beta(close)[1],
        "rsi14": round(last_rsi, 1) if last_rsi is not None else None,
        "volume": int(last_volume),
        "avg_volume_20d": int(last_avg_volume) if last_avg_volume else None,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio else None,
        "cross_signal": cross_signal,
        "above_sma50": (last_close > last_sma50) if last_sma50 else None,
        "above_sma200": (last_close > last_sma200) if last_sma200 else None,
        "capex_to_da": round(capex_to_da, 2) if capex_to_da is not None else None,
        "fcf_margin_pct": round(fcf_margin_pct, 1) if fcf_margin_pct is not None else None,
        "sbc_to_revenue_pct": round(sbc_to_revenue_pct, 1) if sbc_to_revenue_pct is not None else None,
        "roic": round(roic_pct, 1) if roic_pct is not None else None,
        "roic_avg": round(roic_avg_pct, 1) if roic_avg_pct is not None else None,
        "roic_min": round(roic_min_pct, 1) if roic_min_pct is not None else None,
        "roic_years": roic_years_count,
        "recommendation_key": recommendation_key if recommendation_key not in (None, "none") else None,
        "num_analysts": num_analysts if isinstance(num_analysts, int) else None,
        "recommendation_breakdown": recommendation_breakdown,
        "target_mean_price": round(target_mean, 2) if isinstance(target_mean, (int, float)) else None,
        "analyst_upside_pct": round(analyst_upside, 1) if analyst_upside is not None else None,
        "pb": round(pb, 2) if isinstance(pb, (int, float)) else None,
        "dividend_yield_pct": round(dividend_yield_pct, 2) if dividend_yield_pct is not None else None,
        "debt_to_equity": round(debt_to_equity, 1) if debt_to_equity is not None else None,
        "market_cap": int(market_cap) if isinstance(market_cap, (int, float)) else None,
        "avg_dollar_volume": round(avg_dollar_volume, 0) if avg_dollar_volume else None,
        "volatility_pct": round(volatility_pct, 1) if volatility_pct is not None else None,
        "beta": round(beta, 2) if isinstance(beta, (int, float)) else None,
        "price_history_3m": price_history_3m,
        "price_history_1y": price_history_1y,
        "valuation": valuation,
    }


def score_buy_candidate(d, extra_weight=0):
    """Poängmodell (0-100) för köpvärdhet. Inte finansiell rådgivning -
    tänkt som ett första filter, inte en slutgiltig sanning.

    Bonuspoäng (allt positivt, inklusive extra_weight om den är positiv)
    och strafpoäng hålls isär under uträkningen. Straffen räknas fullt ut
    som förut - en dålig aktie ska fortsatt kunna hamna nära noll. Men
    bonuspoängen dämpas med avtagande avkastning (de första
    BONUS_FULL_THRESHOLD poängen räknas fullt ut, resten till en bråkdel)
    så att det krävs väsentligt fler samtidiga positiva signaler för att nå
    poäng nära 100 - och detta håller automatiskt även när fler
    bonusfaktorer läggs till i framtiden, utan att varje enskild vikt
    behöver sänkas manuellt igen.

    extra_weight: geopolitik/makro-vikt (sektor + land), positiv eller
    negativ, som ska vägas in i samma dämpning som resten av bonusarna."""
    bonus = 0
    penalty = 0
    reasons = []

    if d["pe"] is not None:
        if 0 < d["pe"] < 15:
            bonus += 10
            reasons.append(f"Lågt P/E ({d['pe']})")
        elif d["pe"] > 40:
            penalty += 15
            reasons.append(f"Högt P/E ({d['pe']})")
    else:
        reasons.append("P/E saknas (t.ex. förlust eller ej rapporterat)")

    if d.get("peg_ratio") is not None:
        if 0 < d["peg_ratio"] < 1:
            bonus += 8
            reasons.append(f"Lågt PEG-tal ({d['peg_ratio']}) – P/E ser rimligt ut i relation till förväntad vinsttillväxt")
        elif d["peg_ratio"] > 3:
            penalty += 10
            reasons.append(f"Högt PEG-tal ({d['peg_ratio']}) – dyrt även efter hänsyn till förväntad tillväxt")

    if d.get("forward_pe_trend_pct") is not None:
        if d["forward_pe_trend_pct"] < -15:
            bonus += 8
            reasons.append(f"Forward P/E {d['forward_pe_trend_pct']:+.0f}% under historiskt P/E – vinsttillväxt väntas")
        elif d["forward_pe_trend_pct"] > 15:
            penalty += 10
            reasons.append(f"Forward P/E {d['forward_pe_trend_pct']:+.0f}% över historiskt P/E – vinstnedgång väntas")

    if d["rsi14"] is not None:
        if d["rsi14"] < 35:
            bonus += 10
            reasons.append(f"RSI lågt/översålt ({d['rsi14']})")
        elif d["rsi14"] > 70:
            penalty += 20
            reasons.append(f"RSI högt/överköpt ({d['rsi14']})")

    if d["cross_signal"] == "golden_cross":
        bonus += 15
        reasons.append("Golden cross (SMA50 korsade upp genom SMA200)")
    elif d["cross_signal"] == "death_cross":
        penalty += 20
        reasons.append("Death cross (SMA50 korsade ner genom SMA200)")

    if d["above_sma50"] and d["above_sma200"]:
        bonus += 8
        reasons.append("Pris över både SMA50 och SMA200 (uppåttrend)")
    elif d["above_sma50"] is False and d["above_sma200"] is False:
        penalty += 10
        reasons.append("Pris under både SMA50 och SMA200 (nedåttrend)")

    if d["volume_ratio"] and d["volume_ratio"] > 2:
        bonus += 7
        reasons.append(f"Kraftigt förhöjd volym ({d['volume_ratio']}x snitt) – möjlig större rörelse")

    rec = d.get("recommendation_key")
    if rec == "strong_buy":
        bonus += 18
        reasons.append(f"Analytikerkonsensus: starkt köp ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "buy":
        bonus += 12
        reasons.append(f"Analytikerkonsensus: köp ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "hold":
        penalty += 5
        reasons.append(f"Analytikerkonsensus: håll ({d.get('num_analysts') or '?'} analytiker) – analytikerna ser varken tydlig upp- eller nedsida")
    elif rec == "underperform":
        penalty += 12
        reasons.append(f"Analytikerkonsensus: undervikta ({d.get('num_analysts') or '?'} analytiker) – analytikerna tror på sämre utveckling än marknaden")
    elif rec == "sell":
        penalty += 18
        reasons.append(f"Analytikerkonsensus: sälj ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "strong_sell":
        penalty += 25
        reasons.append(f"Analytikerkonsensus: starkt sälj ({d.get('num_analysts') or '?'} analytiker)")

    upside = d.get("analyst_upside_pct")
    if upside is not None:
        if upside > 15:
            bonus += 8
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% över dagens pris")
        elif upside < -10:
            penalty += 10
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% under dagens pris")
        elif upside < 0:
            penalty += 5
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% under dagens pris (måttligt)")

    if d.get("pb") is not None:
        if d["pb"] < 0:
            penalty += 25
            reasons.append(f"Negativt P/B ({d['pb']}) – bolaget har negativt eget kapital, allvarlig varningssignal")
        elif 0 < d["pb"] < 1.5:
            high_leverage = d.get("debt_to_equity") is not None and d["debt_to_equity"] > 100
            if high_leverage:
                reasons.append(f"Lågt P/B ({d['pb']}) men hög skuldsättning – kan vara en värdefälla snarare än ett fynd, ingen poängbonus")
            else:
                bonus += 8
                reasons.append(f"Lågt P/B ({d['pb']}) – handlas nära/under bokfört värde")
        elif d["pb"] > 6:
            penalty += 10
            reasons.append(f"Högt P/B ({d['pb']})")

    if d.get("dividend_yield_pct") is not None:
        rf = d.get("risk_free_rate_pct")
        if rf is not None:
            if d["dividend_yield_pct"] > rf + 1:
                bonus += 5
                reasons.append(f"Utdelning {d['dividend_yield_pct']}% ger meningsfullt mer än riskfri ränta ({rf}%)")
            elif d["dividend_yield_pct"] < rf - 2 and d["dividend_yield_pct"] > 0:
                reasons.append(f"Utdelning {d['dividend_yield_pct']}% ger klart mindre än riskfri ränta ({rf}%) – ingen poäng för det")
        elif d["dividend_yield_pct"] > 3:
            bonus += 5
            reasons.append(f"Utdelning {d['dividend_yield_pct']}%")

    if d.get("risk_premium_pct") is not None:
        if d["risk_premium_pct"] > 8:
            bonus += 10
            reasons.append(f"Hög riskpremie (vinstavkastning {d['earnings_yield_pct']}% mot riskfri ränta {d['risk_free_rate_pct']}%) – betydligt mer betalt för risken än en säker placering ger")
        elif d["risk_premium_pct"] < 0:
            penalty += 10
            reasons.append(f"Negativ riskpremie (vinstavkastning {d['earnings_yield_pct']}% under riskfri ränta {d['risk_free_rate_pct']}%) – du får MER avkastning helt riskfritt just nu")

    if d.get("revenue_growth_yoy_pct") is not None:
        if d["revenue_growth_yoy_pct"] > 15:
            bonus += 8
            reasons.append(f"Stark intäktstillväxt ({d['revenue_growth_yoy_pct']:+.0f}% mot samma kvartal förra året)")
        elif d["revenue_growth_yoy_pct"] < -5:
            penalty += 8
            reasons.append(f"Krympande intäkter ({d['revenue_growth_yoy_pct']:+.0f}% mot samma kvartal förra året)")

    if d.get("operating_margin_trend_pp") is not None:
        if d["operating_margin_trend_pp"] > 3:
            bonus += 8
            reasons.append(f"Förbättrad rörelsemarginal ({d['operating_margin_trend_pp']:+.1f} procentenheter mot samma kvartal förra året)")
        elif d["operating_margin_trend_pp"] < -3:
            penalty += 8
            reasons.append(f"Försämrad rörelsemarginal ({d['operating_margin_trend_pp']:+.1f} procentenheter mot samma kvartal förra året)")

    if d.get("debt_to_equity") is not None:
        if d["debt_to_equity"] < 50:
            bonus += 5
            reasons.append(f"Låg skuldsättning (D/E {d['debt_to_equity']})")
        elif d["debt_to_equity"] > 150:
            penalty += 10
            reasons.append(f"Hög skuldsättning (D/E {d['debt_to_equity']})")

    # Kombinationssignal för finansiell stress: ingen vinst + hög
    # skuldsättning + nedåttrend samtidigt är ett starkare varningstecken
    # än vad de tre faktorerna signalerar var för sig.
    if (
        d["pe"] is None
        and d.get("debt_to_equity") is not None and d["debt_to_equity"] > 120
        and d["above_sma50"] is False
    ):
        penalty += 15
        reasons.append("Kombination av utebliven vinst, hög skuldsättning och nedåttrend – tecken på finansiell stress")

    # Kvalitetsspärrar: mikro-cap och illikvida aktier ger opålitliga
    # tekniska signaler (SMA/RSI blir brus vid tunn handel) och extra risk.
    if d.get("market_cap") is not None:
        if d["market_cap"] < 50_000_000:
            penalty += 25
            reasons.append("Mikro-cap (<50M i börsvärde) – hög risk, tunn handel gör tekniska signaler opålitliga")
        elif d["market_cap"] < 300_000_000:
            penalty += 10
            reasons.append("Litet börsvärde (<300M) – högre risk och volatilitet än genomsnittet")

    if d.get("avg_dollar_volume") is not None and d["avg_dollar_volume"] < 100_000:
        penalty += 20
        reasons.append("Extremt låg likviditet (<100k i daglig omsättning) – svårt att handla utan att flytta kursen")

    if d.get("volatility_pct") is not None:
        if d["volatility_pct"] > 80:
            penalty += 15
            reasons.append(f"Mycket hög volatilitet ({d['volatility_pct']}% årstakt) – stora, oregelbundna kurssvängningar")
        elif d["volatility_pct"] > 45:
            penalty += 5
            reasons.append(f"Förhöjd volatilitet ({d['volatility_pct']}% årstakt)")
        elif d["volatility_pct"] < 20:
            bonus += 5
            reasons.append(f"Låg volatilitet ({d['volatility_pct']}% årstakt) – stabil kursutveckling")

    if d.get("fcf_margin_pct") is not None:
        if d["fcf_margin_pct"] < 0:
            penalty += 15
            reasons.append(f"Negativ FCF-marginal ({d['fcf_margin_pct']}%) – bolaget bränner kassa")
        elif d["fcf_margin_pct"] > 15:
            bonus += 8
            reasons.append(f"Stark FCF-marginal ({d['fcf_margin_pct']}%) – genererar gott om fritt kassaflöde")

    # ROIC (flerårigt snitt). Max ±6 poäng totalt - ska inte dominera över
    # övriga kvalitetsmått som FCF-marginal. Ej tillämpligt för banker/
    # försäkring/fastighetsbolag (roic_na), och kräver minst 3 giltiga år
    # (roic_avg är bara satt om så är fallet) - annars neutralt, inget avdrag.
    if d.get("roic_na"):
        pass
    elif d.get("roic_avg") is not None:
        avg = d["roic_avg"]
        years = d.get("roic_years", 0)
        roic_adj = 0
        if avg > 15:
            roic_adj += 5
            reasons.append(f"Stark genomsnittlig avkastning på investerat kapital (ROIC-snitt {avg:.1f}% över {years} år)")
        elif avg >= 10:
            roic_adj += 3
            reasons.append(f"God genomsnittlig ROIC ({avg:.1f}% över {years} år)")
        elif avg < 6:
            roic_adj -= 4
            reasons.append(f"Svag genomsnittlig ROIC ({avg:.1f}% över {years} år) – omvandlar kapital till vinst mindre effektivt")

        rmin = d.get("roic_min")
        if rmin is not None:
            if rmin > 8:
                roic_adj += 2
                reasons.append(f"Stabil ROIC – även sämsta året över {rmin:.1f}%")
            elif (avg - rmin) > 15:
                roic_adj -= 2
                reasons.append(f"Ostabil ROIC – stora svängningar mellan åren (sämsta året {rmin:.1f}%)")

        roic_adj = max(-6, min(6, roic_adj))
        if roic_adj > 0:
            bonus += roic_adj
        elif roic_adj < 0:
            penalty += -roic_adj

    if d.get("sbc_to_revenue_pct") is not None and d["sbc_to_revenue_pct"] > 15:
        penalty += 10
        reasons.append(f"Hög aktiebaserad ersättning ({d['sbc_to_revenue_pct']}% av intäkter) – utspädningsrisk, sänker kvaliteten på redovisad vinst")

    if d.get("capex_to_da") is not None and d["capex_to_da"] > 5:
        penalty += 5
        reasons.append(f"Mycket hög investeringstakt (capex {d['capex_to_da']}x avskrivningar) – aggressiv tillväxtfas, ökad osäkerhet kring avkastning")

    if extra_weight > 0:
        bonus += extra_weight
    elif extra_weight < 0:
        penalty += -extra_weight

    BONUS_FULL_THRESHOLD = 25   # dessa första poängen räknas fullt ut
    DIMINISHING_RATE = 0.4      # allt därutöver räknas bara till 40%
    if bonus > BONUS_FULL_THRESHOLD:
        effective_bonus = BONUS_FULL_THRESHOLD + (bonus - BONUS_FULL_THRESHOLD) * DIMINISHING_RATE
    else:
        effective_bonus = bonus

    score = 50 + effective_bonus - penalty
    return max(0, min(100, round(score))), reasons


def score_growth_candidate(d, extra_weight=0):
    """Alternativ poängmodell (0-100) för unga tillväxtbolag (100M-1md USD
    börsvärde), byggd för fliken 'Tillväxtbolag'. Samma princip som
    score_buy_candidate (bas 50, dämpade bonusar, fulla straff) men med
    medvetet omkalibrerade vikter:

    - Mikro-cap-straffet är borttaget helt - litenhet är väntat här, inte
      ett fel, eftersom hela fliken redan bara visar bolag inom bandet.
    - Volatilitet ger varken bonus eller straff - hög volatilitet är
      normalt för unga bolag, inte en varningssignal i sig.
    - Tillväxt (intäkter/marginal) och momentum (volym, RSI, tidig
      SMA20/50-signal) väger betydligt tyngre än i standardmodellen.
    - Likviditetsspärrar, finansiell stress-kombon och negativt P/B är
      OFÖRÄNDRADE - det är skillnad på "litet men handlingsbart" och
      "praktiskt taget infångad om du köper", och den skillnaden ska inte
      viktas bort bara för att lyfta potential."""
    bonus = 0
    penalty = 0
    reasons = []

    if d["pe"] is not None:
        if 0 < d["pe"] < 15:
            bonus += 10
            reasons.append(f"Lågt P/E ({d['pe']})")
        elif d["pe"] > 40:
            penalty += 15
            reasons.append(f"Högt P/E ({d['pe']})")
    else:
        reasons.append("P/E saknas (vanligt för unga bolag utan stabil vinst)")

    if d.get("peg_ratio") is not None:
        if 0 < d["peg_ratio"] < 1:
            bonus += 8
            reasons.append(f"Lågt PEG-tal ({d['peg_ratio']}) – P/E ser rimligt ut i relation till förväntad vinsttillväxt")
        elif d["peg_ratio"] > 3:
            penalty += 10
            reasons.append(f"Högt PEG-tal ({d['peg_ratio']}) – dyrt även efter hänsyn till förväntad tillväxt")

    if d.get("forward_pe_trend_pct") is not None:
        if d["forward_pe_trend_pct"] < -15:
            bonus += 8
            reasons.append(f"Forward P/E {d['forward_pe_trend_pct']:+.0f}% under historiskt P/E – vinsttillväxt väntas")
        elif d["forward_pe_trend_pct"] > 15:
            penalty += 10
            reasons.append(f"Forward P/E {d['forward_pe_trend_pct']:+.0f}% över historiskt P/E – vinstnedgång väntas")

    if d["rsi14"] is not None:
        if d["rsi14"] < 35:
            bonus += 14
            reasons.append(f"RSI lågt/översålt ({d['rsi14']}) – momentumsignal")
        elif d["rsi14"] > 70:
            penalty += 12
            reasons.append(f"RSI högt/överköpt ({d['rsi14']})")

    if d["cross_signal"] == "golden_cross":
        bonus += 15
        reasons.append("Golden cross (SMA50 korsade upp genom SMA200)")
    elif d["cross_signal"] == "death_cross":
        penalty += 20
        reasons.append("Death cross (SMA50 korsade ner genom SMA200)")

    if d.get("cross_signal_20_50") == "golden_cross":
        bonus += 10
        reasons.append("Tidig momentumsignal: SMA20 korsade upp genom SMA50")
    elif d.get("cross_signal_20_50") == "death_cross":
        penalty += 10
        reasons.append("Tidig varningssignal: SMA20 korsade ner genom SMA50")

    if d["above_sma50"] and d["above_sma200"]:
        bonus += 8
        reasons.append("Pris över både SMA50 och SMA200 (uppåttrend)")
    elif d["above_sma50"] is False and d["above_sma200"] is False:
        penalty += 10
        reasons.append("Pris under både SMA50 och SMA200 (nedåttrend)")

    if d["volume_ratio"] and d["volume_ratio"] > 2:
        bonus += 12
        reasons.append(f"Kraftigt förhöjd volym ({d['volume_ratio']}x snitt) – momentumsignal, möjligt genombrott")

    rec = d.get("recommendation_key")
    if rec == "strong_buy":
        bonus += 18
        reasons.append(f"Analytikerkonsensus: starkt köp ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "buy":
        bonus += 12
        reasons.append(f"Analytikerkonsensus: köp ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "hold":
        penalty += 5
        reasons.append(f"Analytikerkonsensus: håll ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "underperform":
        penalty += 12
        reasons.append(f"Analytikerkonsensus: undervikta ({d.get('num_analysts') or '?'} analytiker) – analytikerna tror på sämre utveckling än marknaden")
    elif rec == "sell":
        penalty += 18
        reasons.append(f"Analytikerkonsensus: sälj ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "strong_sell":
        penalty += 25
        reasons.append(f"Analytikerkonsensus: starkt sälj ({d.get('num_analysts') or '?'} analytiker)")
    else:
        reasons.append("Ingen/mycket begränsad analytikerbevakning (vanligt för unga bolag)")

    upside = d.get("analyst_upside_pct")
    if upside is not None:
        if upside > 15:
            bonus += 10
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% över dagens pris")
        elif upside < -10:
            penalty += 10
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% under dagens pris")

    if d.get("pb") is not None:
        if d["pb"] < 0:
            penalty += 25
            reasons.append(f"Negativt P/B ({d['pb']}) – bolaget har negativt eget kapital, allvarlig varningssignal")
        elif 0 < d["pb"] < 1.5:
            high_leverage = d.get("debt_to_equity") is not None and d["debt_to_equity"] > 100
            if high_leverage:
                reasons.append(f"Lågt P/B ({d['pb']}) men hög skuldsättning – kan vara en värdefälla, ingen poängbonus")
            else:
                bonus += 8
                reasons.append(f"Lågt P/B ({d['pb']}) – handlas nära/under bokfört värde")
        elif d["pb"] > 6:
            penalty += 8
            reasons.append(f"Högt P/B ({d['pb']}) – kan vara rimligt för ett snabbväxande bolag, men innebär hög värderingsrisk")

    if d.get("risk_premium_pct") is not None:
        if d["risk_premium_pct"] > 8:
            bonus += 8
            reasons.append(f"Hög riskpremie (vinstavkastning {d['earnings_yield_pct']}% mot riskfri ränta {d['risk_free_rate_pct']}%)")
        elif d["risk_premium_pct"] < 0:
            penalty += 8
            reasons.append(f"Negativ riskpremie (vinstavkastning {d['earnings_yield_pct']}% under riskfri ränta {d['risk_free_rate_pct']}%)")

    if d.get("revenue_growth_yoy_pct") is not None:
        if d["revenue_growth_yoy_pct"] > 15:
            bonus += 16
            reasons.append(f"Stark intäktstillväxt ({d['revenue_growth_yoy_pct']:+.0f}% mot samma kvartal förra året)")
        elif d["revenue_growth_yoy_pct"] < -5:
            penalty += 12
            reasons.append(f"Krympande intäkter ({d['revenue_growth_yoy_pct']:+.0f}% mot samma kvartal förra året) – varningstecken för ett bolag som ska växa")

    if d.get("operating_margin_trend_pp") is not None:
        if d["operating_margin_trend_pp"] > 3:
            bonus += 14
            reasons.append(f"Förbättrad rörelsemarginal ({d['operating_margin_trend_pp']:+.1f} procentenheter mot samma kvartal förra året) – tecken på skalbarhet")
        elif d["operating_margin_trend_pp"] < -3:
            penalty += 10
            reasons.append(f"Försämrad rörelsemarginal ({d['operating_margin_trend_pp']:+.1f} procentenheter mot samma kvartal förra året)")

    if d.get("debt_to_equity") is not None:
        if d["debt_to_equity"] < 50:
            bonus += 5
            reasons.append(f"Låg skuldsättning (D/E {d['debt_to_equity']})")
        elif d["debt_to_equity"] > 150:
            penalty += 10
            reasons.append(f"Hög skuldsättning (D/E {d['debt_to_equity']}) – extra riskabelt för ett litet bolag")

    # Finansiell stress-kombo och likviditetsspärrar - OFÖRÄNDRADE mot
    # standardmodellen. Litenhet ursäktar inte genuina varningstecken.
    if (
        d["pe"] is None
        and d.get("debt_to_equity") is not None and d["debt_to_equity"] > 120
        and d["above_sma50"] is False
    ):
        penalty += 15
        reasons.append("Kombination av utebliven vinst, hög skuldsättning och nedåttrend – tecken på finansiell stress")

    if d.get("avg_dollar_volume") is not None and d["avg_dollar_volume"] < 100_000:
        penalty += 20
        reasons.append("Extremt låg likviditet (<100k i daglig omsättning) – svårt att handla utan att flytta kursen, oavsett tillväxtpotential")

    # OBS: volatilitet ger varken bonus eller straff här - hög volatilitet
    # är väntat för unga bolag, ingen egen signal i den här modellen.

    if d.get("fcf_margin_pct") is not None:
        if d["fcf_margin_pct"] < 0:
            penalty += 10
            reasons.append(f"Negativ FCF-marginal ({d['fcf_margin_pct']}%) – vanligt i tillväxtfas, men bevaka kassans räckvidd")
        elif d["fcf_margin_pct"] > 15:
            bonus += 8
            reasons.append(f"Stark FCF-marginal ({d['fcf_margin_pct']}%) – ovanligt moget kassaflöde för bolagets storlek")

    # ROIC (flerårigt snitt) - samma underliggande beräkning som köpmodellen,
    # men mer överseende: unga tillväxtbolag förväntas ofta ha låg/negativ
    # ROIC medan de investerar för att växa, så bara kraftigt negativa
    # flerårssnitt straffas här. Ej tillämpligt för banker/försäkring/
    # fastighetsbolag (roic_na); kräver minst 3 giltiga år för att ges alls.
    if d.get("roic_na"):
        pass
    elif d.get("roic_avg") is not None:
        if d["roic_avg"] > 15:
            bonus += 8
            reasons.append(f"Stark genomsnittlig avkastning på investerat kapital (ROIC-snitt {d['roic_avg']:.1f}% över {d.get('roic_years',0)} år) – ovanligt moget för bolagets storlek")
        elif d["roic_avg"] < -10:
            penalty += 8
            reasons.append(f"Kraftigt negativ genomsnittlig ROIC ({d['roic_avg']:.1f}%) – måttligt negativt är normalt i tillväxtfas, men den här nivån är en varningssignal")

    if d.get("sbc_to_revenue_pct") is not None and d["sbc_to_revenue_pct"] > 15:
        penalty += 8
        reasons.append(f"Hög aktiebaserad ersättning ({d['sbc_to_revenue_pct']}% av intäkter) – utspädningsrisk")

    if extra_weight > 0:
        bonus += extra_weight
    elif extra_weight < 0:
        penalty += -extra_weight

    BONUS_FULL_THRESHOLD = 25
    DIMINISHING_RATE = 0.4
    if bonus > BONUS_FULL_THRESHOLD:
        effective_bonus = BONUS_FULL_THRESHOLD + (bonus - BONUS_FULL_THRESHOLD) * DIMINISHING_RATE
    else:
        effective_bonus = bonus

    score = 50 + effective_bonus - penalty
    return max(0, min(100, round(score))), reasons


def get_app_version() -> str:
    """Läser det klassiska löpnumret från VERSION-filen (t.ex. '1.0') för
    visning i dashboardens versionsindikator. Filen uppdateras manuellt när
    en ny funktion/version släpps - inte per körning."""
    version_file = ROOT / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except Exception:
        return "okänd"


def sanitize_for_json(obj):
    """Rekursiv säkerhetsspärr: ersätter NaN/Infinity (giltiga Python-flyttal
    men INTE giltig JSON enligt spec) med None, så att resultatfilen aldrig
    kan bli trasig JSON som kraschar JSON.parse() i webbläsaren - oavsett
    var i koden ett sådant värde skulle uppstå."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


def stability_adjustment(bonds):
    """Bred, måttlig poängjustering från ränte-/kreditstabilitetspoängen (0-100).
    Nivå: >=80 +2, 60-79 0, 50-59 -2, 40-49 -4, 30-39 -6, <30 -8.
    Trend (mot för ~1 månad sedan): fall på >=5 poäng -2, ökning på >=5 poäng
    +2 (bara om nivån är minst 50). Returnerar (justering, anteckningar)."""
    st = (bonds or {}).get("stability") or {}
    score, delta = st.get("score"), st.get("delta")
    if score is None:
        return 0, []
    if score >= 80:
        adj = 2
    elif score >= 60:
        adj = 0
    elif score >= 50:
        adj = -2
    elif score >= 40:
        adj = -4
    elif score >= 30:
        adj = -6
    else:
        adj = -8
    notes = []
    if adj:
        notes.append(f"Ränte- och kreditstabilitet {score:.0f}/100 ({st.get('label')}) – {'stödjande' if adj > 0 else 'försiktighetsjustering'} för alla aktier")
    if delta is not None and delta <= -5:
        adj -= 2
        notes.append(f"Stabiliteten har försämrats {abs(delta):.0f} poäng på en månad")
    elif delta is not None and delta >= 5 and score >= 50:
        adj += 2
        notes.append(f"Stabiliteten har förbättrats {delta:.0f} poäng på en månad")
    return adj, notes


def main():
    watchlist = load_watchlist()
    sector_weights, sector_factor_names, country_weights, country_factor_names = load_risk_factors()
    risk_free_rates = load_risk_free_rates()
    investtech_top20 = load_investtech_top20()

    macro = compute_macro_indicators()
    try:
        bonds, dgs10_rows = compute_bond_dashboard()
        set_rate_series(dgs10_rows)
        macro["bonds"] = bonds
    except Exception as e:
        print(f"Obligationsöversikt misslyckades: {type(e).__name__}: {e}", file=sys.stderr)
        macro["bonds"] = None
    macro_weight = 0
    macro_notes = []
    if macro.get("yield_curve_inverted") and macro.get("yield_curve_inverted_days", 0) >= 30:
        macro_weight -= 5
        macro_notes.append(
            f"Inverterad räntekurva i {macro['yield_curve_inverted_days']} dagar (10år-2år: {macro['yield_curve_10y2y_pct']}pp) – bred makroförsiktighet, historiskt ett tidigt recessionstecken"
        )
    if macro.get("credit_spread_level") == "forhojd":
        macro_weight -= 5
        macro_notes.append(
            f"Förhöjda kreditspreadar (high-yield {macro['credit_spread_hy_pct']}pp) – obligationsmarknaden prisar in ökad risk"
        )
    elif macro.get("credit_spread_level") == "kris":
        macro_weight -= 10
        macro_notes.append(
            f"Kraftigt förhöjda kreditspreadar (high-yield {macro['credit_spread_hy_pct']}pp) – krisliknande nivå på obligationsmarknaden"
        )
    stab_adj, stab_notes = stability_adjustment(macro.get("bonds"))
    macro_weight += stab_adj
    macro_notes.extend(stab_notes)
    macro_weight = max(-12, min(4, macro_weight))   # tak: räntekurva/kredit ingår redan i stabilitetspoängen
    macro["stability_adjustment"] = stab_adj
    macro["score_adjustment"] = macro_weight   # visas i MAKRO-panelen: poängpåverkan på alla aktier
    macro["score_notes"] = macro_notes
    score_history = load_score_history()
    today_str = datetime.now(timezone.utc).date().isoformat()

    results = []
    for entry in watchlist:
        ticker = entry["ticker"]
        print(f"Hämtar {ticker}...", file=sys.stderr)
        try:
            d = analyze_ticker(ticker)
        except Exception as e:
            print(f"  Fel vid hämtning av {ticker}: {e}", file=sys.stderr)
            d = None

        if d is None:
            continue

        try:
            d["market"] = entry["market"]
            d["watchlist_name"] = entry["name"]
            d["sector"] = entry.get("sector")
            d["country"] = entry.get("country")
            d["growth_candidate"] = bool(entry.get("growth_candidate"))
            # ROIC är inte ett meningsfullt mått för banker/försäkring/
            # fastighetsbolag - deras kapitalstruktur skiljer sig strukturellt
            # från vanliga rörelsedrivande bolag. Ges neutral poäng istället.
            d["roic_na"] = d["sector"] in ("Financials", "RealEstate")
            d["risk_free_rate_pct"] = risk_free_rates.get(entry["market"])
            if d["risk_free_rate_pct"] is not None and isinstance(d.get("pe"), (int, float)) and d["pe"] > 0:
                d["earnings_yield_pct"] = round(100 / d["pe"], 2)
                d["risk_premium_pct"] = round(d["earnings_yield_pct"] - d["risk_free_rate_pct"], 2)
            else:
                d["earnings_yield_pct"] = None
                d["risk_premium_pct"] = None
            d["data_source"] = "yfinance"

            if entry["market"] == "US":
                fmp_data = fetch_fmp_fundamentals(ticker)
                if fmp_data:
                    d.update(fmp_data)
                    d["data_source"] = "yfinance+fmp"

            sector = entry.get("sector")
            sector_weight = sector_weights.get(sector, 0) if sector else 0
            country_weight = country_weights.get(entry["market"], 0)

            investtech_entry = investtech_top20.get(ticker)
            investtech_weight = 0
            if investtech_entry:
                rank = investtech_entry["rank"]
                investtech_weight = 10 if rank <= 5 else (7 if rank <= 10 else 5)

            valuation_w, valuation_note = valuation_weight(d.get("valuation"), sector)
            d["valuation_weight"] = valuation_w
            total_extra = sector_weight + country_weight + investtech_weight + macro_weight + valuation_w
            buy_score, buy_reasons = score_buy_candidate(d, extra_weight=total_extra)
            growth_score, growth_reasons = score_growth_candidate(d, extra_weight=total_extra)

            geopolitics_note = None
            if sector_weight:
                geopolitics_note = ", ".join(sector_factor_names.get(sector, []))
                buy_reasons.append(f"Geopolitik/makro ({sector}): {geopolitics_note}")
                growth_reasons.append(f"Geopolitik/makro ({sector}): {geopolitics_note}")

            geopolitics_country_note = None
            if country_weight:
                geopolitics_country_note = ", ".join(country_factor_names.get(entry["market"], []))
                buy_reasons.append(f"Geopolitik/makro ({entry.get('country')}): {geopolitics_country_note}")
                growth_reasons.append(f"Geopolitik/makro ({entry.get('country')}): {geopolitics_country_note}")

            if investtech_entry:
                note = f"Investtech Topp 20 (plats #{investtech_entry['rank']}, teknisk poäng {investtech_entry['investtech_score']}) – teknisk medelfristig signal (1-6 mån)"
                buy_reasons.append(note)
                growth_reasons.append(note)

            if valuation_note:
                buy_reasons.append(valuation_note)
                growth_reasons.append(valuation_note)

            for macro_note in macro_notes:
                buy_reasons.append(macro_note)
                growth_reasons.append(macro_note)

            d["buy_score"] = buy_score
            d["buy_reasons"] = buy_reasons
            d["growth_score"] = growth_score
            d["growth_reasons"] = growth_reasons
            d["investtech_rank"] = investtech_entry["rank"] if investtech_entry else None
            # Exponerar den använda sektor- och landsvikten (+ förklaringstexter)
            # så att webbläsaren kan räkna ut en identisk geopolitik-justering
            # för säljpoängen, som numera beräknas helt klientsidan (innehav
            # skickas aldrig till servern).
            d["sector_weight"] = sector_weight
            d["geopolitics_note"] = geopolitics_note
            d["country_weight"] = country_weight
            d["geopolitics_country_note"] = geopolitics_country_note
            d["buy_days_on_list"], d["buy_score_delta"] = update_score_history(
                score_history, ticker, buy_score, today_str, price=d.get("price")
            )

            # "Ny på listan"-markering: om den tidigaste posten i
            # poänghistoriken för denna ticker är från de senaste dagarna
            # har den nyligen lagts till i bevakningslistan.
            NEW_TICKER_WINDOW_DAYS = 5
            try:
                buy_history = score_history.get(ticker, {}).get("buy", [])
                if buy_history:
                    first_seen = datetime.strptime(buy_history[0]["date"], "%Y-%m-%d").date()
                    today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
                    d["is_new"] = (today_date - first_seen).days <= NEW_TICKER_WINDOW_DAYS
                else:
                    d["is_new"] = True
            except Exception:
                d["is_new"] = False

            results.append(d)
        except Exception as e:
            print(f"  Bearbetning/poängsättning misslyckades för {ticker}: {type(e).__name__}: {e}", file=sys.stderr)

        time.sleep(0.3)  # snäll mot Yahoo Finance

    save_score_history(score_history)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": get_app_version(),
        "count": len(results),
        "macro": macro,
        "geopolitics": load_geopolitics_export(sector_weights, country_weights),
        "risk_free": load_risk_free_export(),
        "results": results,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    output = sanitize_for_json(output)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Klar: {len(results)} tickers analyserade, skrivet till {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        try:
            with open(ROOT / "debug_crash.txt", "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        # Avslutar ändå med kod 0 så att workflowens commit-steg körs och
        # felrapporten faktiskt går att läsa via GitHub Contents API
        # (Actions-loggarna är inte läsbara med nuvarande token-behörighet).
        # TILLFÄLLIG DIAGNOSTIK - ta bort när roten till felet är hittad.
