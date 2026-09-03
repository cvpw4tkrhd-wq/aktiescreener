#!/usr/bin/env python3
"""
Aktiescreener för svenska och amerikanska börsen.

Hämtar kursdata via yfinance, beräknar P/E, SMA50/200, RSI14 och
volymavvikelser, och genererar köpkandidater samt säljsignaler för
befintliga innehav (importerade från Avanza/Nordnet-CSV).

Körs antingen manuellt: python scripts/screener.py
eller schemalagt via GitHub Actions (.github/workflows/screener.yml)
"""

import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
WATCHLIST_FILE = DATA_DIR / "watchlist.yml"
HOLDINGS_FILE = DATA_DIR / "holdings.csv"
PREVIOUSLY_HELD_FILE = DATA_DIR / "previously_held.yml"
SCORE_HISTORY_FILE = DATA_DIR / "score_history.json"
OUTPUT_FILE = DOCS_DIR / "results.json"

BUY_SIGNAL_THRESHOLD = 65   # köppoäng för att räknas som "aktiv köpsignal" i dagräkningen
SELL_SIGNAL_THRESHOLD = 50  # säljpoäng för att räknas som "aktiv säljsignal" i dagräkningen
MAX_HISTORY_ENTRIES = 90    # ca 4 månaders vardagskörningar per ticker/typ

RSI_PERIOD = 14
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
        })
    return tickers


RISK_FACTORS_FILE = DATA_DIR / "risk_factors.yml"


def load_risk_factors():
    """Läser redigerbara geopolitiska/makro-riskfaktorer och summerar vikt per
    sektor. Returnerar (sector_weights: dict, active_factor_names: dict sector->[namn])."""
    if not RISK_FACTORS_FILE.exists():
        return {}, {}
    with open(RISK_FACTORS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    sector_weights = {}
    sector_factor_names = {}
    for factor in data.get("factors") or []:
        for sector, weight in (factor.get("sectors") or {}).items():
            sector_weights[sector] = sector_weights.get(sector, 0) + weight
            sector_factor_names.setdefault(sector, []).append(f"{factor.get('name','?')} ({weight:+d})")
    return sector_weights, sector_factor_names


def load_previously_held():
    """Läser historik över tidigare/nuvarande innehav (bara ticker/namn/datum,
    aldrig belopp eller antal). Returnerar {ticker: {..}}."""
    if not PREVIOUSLY_HELD_FILE.exists():
        return {}
    with open(PREVIOUSLY_HELD_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("positions") or {}


def save_previously_held(positions: dict):
    today = datetime.now(timezone.utc).date().isoformat()
    header = (
        "# Auto-uppdaterad av screener.py — spårar vilka tickers du någon\n"
        "# gång ägt (enligt holdings.csv) så de kan flaggas som möjliga\n"
        "# återköpskandidater i köplistan om du säljer och de senare ser\n"
        "# köpvärda ut igen. Innehåller BARA ticker, bolagsnamn, sektor och\n"
        "# datum — aldrig belopp eller antal aktier.\n"
        f"# Senast uppdaterad: {today}\n\n"
    )
    body = yaml.safe_dump({"positions": positions}, allow_unicode=True, sort_keys=True)
    with open(PREVIOUSLY_HELD_FILE, "w", encoding="utf-8") as f:
        f.write(header + body)


def update_previously_held(positions: dict, current_holding_entries: list, holdings_loaded: int):
    """Uppdaterar historikfilen utifrån dagens körning. Rör INGET om
    holdings.csv inte fanns med i körningen (holdings_loaded == 0), för att
    inte av misstag markera allt som sålt bara för att secreten saknades."""
    if holdings_loaded == 0:
        return positions, False

    today = datetime.now(timezone.utc).date().isoformat()
    current_tickers = {e["ticker"] for e in current_holding_entries}

    for e in current_holding_entries:
        rec = positions.get(e["ticker"], {})
        rec["name"] = e["name"]
        rec["sector"] = e.get("sector")
        rec["status"] = "held"
        rec.setdefault("first_seen", today)
        rec["last_held"] = today
        rec["sold_detected"] = None
        positions[e["ticker"]] = rec

    for ticker, rec in positions.items():
        if rec.get("status") == "held" and ticker not in current_tickers:
            rec["status"] = "sold"
            rec["sold_detected"] = today

    return positions, True


def load_score_history():
    if not SCORE_HISTORY_FILE.exists():
        return {}
    with open(SCORE_HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_score_history(history: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(SCORE_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, sort_keys=True)


def update_score_history(history: dict, ticker: str, kind: str, score, today_str: str):
    """Lägger till dagens poäng i historiken för en ticker (buy/sell) och
    returnerar (dagar_i_rad_med_aktiv_signal, poängförändring_sedan_föregående_körning).
    Kör man screenern flera gånger samma dag skrivs den dagens post över
    istället för att dubbleras."""
    if score is None:
        return None, None

    threshold = BUY_SIGNAL_THRESHOLD if kind == "buy" else SELL_SIGNAL_THRESHOLD
    series = history.setdefault(ticker, {}).setdefault(kind, [])
    series[:] = [e for e in series if e["date"] != today_str]
    series.append({"date": today_str, "score": score})
    series.sort(key=lambda e: e["date"])
    if len(series) > MAX_HISTORY_ENTRIES:
        del series[: -MAX_HISTORY_ENTRIES]

    delta = series[-1]["score"] - series[-2]["score"] if len(series) >= 2 else None

    days = 0
    for entry in reversed(series):
        if entry["score"] >= threshold:
            days += 1
        else:
            break

    return days, delta


def load_holdings():
    """Läser en Avanza- eller Nordnet-CSV-export och returnerar en dict {ticker: antal}.

    Avanza-export har kolumnen 'Namn' + 'Volym' (eller 'Antal').
    Nordnet-export har 'Verdipapir'/'Værdipapir' + 'Antal'.
    Vi matchar löst på ticker-symbol där det går; annars på namn mot watchlist.
    """
    if not HOLDINGS_FILE.exists():
        return {}

    holdings = {}
    with open(HOLDINGS_FILE, "r", encoding="utf-8-sig") as f:
        # Avanza/Nordnet exports are often semicolon-separated
        sample = f.read(2048)
        f.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            # normalize keys (strip whitespace / BOM)
            row = {k.strip().lower(): v for k, v in row.items() if k}
            name = row.get("namn") or row.get("verdipapir") or row.get("värdipapir") or row.get("name")
            qty_raw = row.get("volym") or row.get("antal") or row.get("quantity")
            ticker = row.get("kortnamn") or row.get("ticker") or row.get("symbol")
            if not name and not ticker:
                continue
            try:
                qty = float(str(qty_raw).replace(",", ".").replace(" ", "")) if qty_raw else None
            except ValueError:
                qty = None
            key = (ticker or name).strip()
            holdings[key] = {"raw_name": name, "raw_ticker": ticker, "quantity": qty}
    return holdings


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

    # Kort prisserie (~3 månader / 63 handelsdagar) för minigraf i dashboarden.
    price_history_3m = [round(float(v), 4) for v in close.tail(63)]

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

    info = {}
    try:
        info = tk.get_info()
    except Exception:
        pass
    pe = info.get("trailingPE")
    forward_pe = info.get("forwardPE")
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
    analyst_upside = None
    if isinstance(target_mean, (int, float)) and last_close:
        analyst_upside = (target_mean - last_close) / last_close * 100

    market_cap = info.get("marketCap")
    beta = info.get("beta")
    avg_dollar_volume = (last_avg_volume * last_close) if (last_avg_volume and last_close) else None

    return {
        "ticker": ticker,
        "name": long_name,
        "currency": currency,
        "price": round(last_close, 2),
        "pe": round(pe, 2) if isinstance(pe, (int, float)) else None,
        "forward_pe": round(forward_pe, 2) if isinstance(forward_pe, (int, float)) else None,
        "sma50": round(last_sma50, 2) if last_sma50 else None,
        "sma200": round(last_sma200, 2) if last_sma200 else None,
        "rsi14": round(last_rsi, 1) if last_rsi is not None else None,
        "volume": int(last_volume),
        "avg_volume_20d": int(last_avg_volume) if last_avg_volume else None,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio else None,
        "cross_signal": cross_signal,
        "above_sma50": (last_close > last_sma50) if last_sma50 else None,
        "above_sma200": (last_close > last_sma200) if last_sma200 else None,
        "recommendation_key": recommendation_key if recommendation_key not in (None, "none") else None,
        "num_analysts": num_analysts if isinstance(num_analysts, int) else None,
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
    }


def score_buy_candidate(d):
    """Enkel poängmodell (0-100) för köpvärdhet. Inte finansiell rådgivning -
    tänkt som ett första filter, inte en slutgiltig sanning."""
    score = 50
    reasons = []

    if d["pe"] is not None:
        if 0 < d["pe"] < 15:
            score += 15
            reasons.append(f"Lågt P/E ({d['pe']})")
        elif d["pe"] > 40:
            score -= 15
            reasons.append(f"Högt P/E ({d['pe']})")
    else:
        reasons.append("P/E saknas (t.ex. förlust eller ej rapporterat)")

    if d["rsi14"] is not None:
        if d["rsi14"] < 35:
            score += 15
            reasons.append(f"RSI lågt/översålt ({d['rsi14']})")
        elif d["rsi14"] > 70:
            score -= 20
            reasons.append(f"RSI högt/överköpt ({d['rsi14']})")

    if d["cross_signal"] == "golden_cross":
        score += 20
        reasons.append("Golden cross (SMA50 korsade upp genom SMA200)")
    elif d["cross_signal"] == "death_cross":
        score -= 20
        reasons.append("Death cross (SMA50 korsade ner genom SMA200)")

    if d["above_sma50"] and d["above_sma200"]:
        score += 10
        reasons.append("Pris över både SMA50 och SMA200 (uppåttrend)")
    elif d["above_sma50"] is False and d["above_sma200"] is False:
        score -= 10
        reasons.append("Pris under både SMA50 och SMA200 (nedåttrend)")

    if d["volume_ratio"] and d["volume_ratio"] > 2:
        score += 10
        reasons.append(f"Kraftigt förhöjd volym ({d['volume_ratio']}x snitt) – möjlig större rörelse")

    rec = d.get("recommendation_key")
    if rec == "strong_buy":
        score += 15
        reasons.append(f"Analytikerkonsensus: starkt köp ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "buy":
        score += 10
        reasons.append(f"Analytikerkonsensus: köp ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "sell":
        score -= 10
        reasons.append(f"Analytikerkonsensus: sälj ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "strong_sell":
        score -= 15
        reasons.append(f"Analytikerkonsensus: starkt sälj ({d.get('num_analysts') or '?'} analytiker)")

    upside = d.get("analyst_upside_pct")
    if upside is not None:
        if upside > 15:
            score += 10
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% över dagens pris")
        elif upside < -10:
            score -= 10
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% under dagens pris")

    if d.get("pb") is not None:
        if d["pb"] < 0:
            score -= 25
            reasons.append(f"Negativt P/B ({d['pb']}) – bolaget har negativt eget kapital, allvarlig varningssignal")
        elif 0 < d["pb"] < 1.5:
            high_leverage = d.get("debt_to_equity") is not None and d["debt_to_equity"] > 100
            if high_leverage:
                reasons.append(f"Lågt P/B ({d['pb']}) men hög skuldsättning – kan vara en värdefälla snarare än ett fynd, ingen poängbonus")
            else:
                score += 10
                reasons.append(f"Lågt P/B ({d['pb']}) – handlas nära/under bokfört värde")
        elif d["pb"] > 6:
            score -= 10
            reasons.append(f"Högt P/B ({d['pb']})")

    if d.get("dividend_yield_pct") is not None and d["dividend_yield_pct"] > 3:
        score += 5
        reasons.append(f"Utdelning {d['dividend_yield_pct']}%")

    if d.get("debt_to_equity") is not None:
        if d["debt_to_equity"] < 50:
            score += 5
            reasons.append(f"Låg skuldsättning (D/E {d['debt_to_equity']})")
        elif d["debt_to_equity"] > 150:
            score -= 10
            reasons.append(f"Hög skuldsättning (D/E {d['debt_to_equity']})")

    # Kombinationssignal för finansiell stress: ingen vinst + hög
    # skuldsättning + nedåttrend samtidigt är ett starkare varningstecken
    # än vad de tre faktorerna signalerar var för sig.
    if (
        d["pe"] is None
        and d.get("debt_to_equity") is not None and d["debt_to_equity"] > 120
        and d["above_sma50"] is False
    ):
        score -= 15
        reasons.append("Kombination av utebliven vinst, hög skuldsättning och nedåttrend – tecken på finansiell stress")

    # Kvalitetsspärrar: mikro-cap och illikvida aktier ger opålitliga
    # tekniska signaler (SMA/RSI blir brus vid tunn handel) och extra risk.
    if d.get("market_cap") is not None:
        if d["market_cap"] < 50_000_000:
            score -= 25
            reasons.append("Mikro-cap (<50M i börsvärde) – hög risk, tunn handel gör tekniska signaler opålitliga")
        elif d["market_cap"] < 300_000_000:
            score -= 10
            reasons.append("Litet börsvärde (<300M) – högre risk och volatilitet än genomsnittet")

    if d.get("avg_dollar_volume") is not None and d["avg_dollar_volume"] < 100_000:
        score -= 20
        reasons.append("Extremt låg likviditet (<100k i daglig omsättning) – svårt att handla utan att flytta kursen")

    if d.get("volatility_pct") is not None:
        if d["volatility_pct"] > 80:
            score -= 15
            reasons.append(f"Mycket hög volatilitet ({d['volatility_pct']}% årstakt) – stora, oregelbundna kurssvängningar")
        elif d["volatility_pct"] > 45:
            score -= 5
            reasons.append(f"Förhöjd volatilitet ({d['volatility_pct']}% årstakt)")
        elif d["volatility_pct"] < 20:
            score += 5
            reasons.append(f"Låg volatilitet ({d['volatility_pct']}% årstakt) – stabil kursutveckling")

    return max(0, min(100, score)), reasons


def _normalize_ticker(s: str) -> str:
    """Normaliserar ett tickernamn för lös matchning: versaler, inga
    mellanslag/bindestreck, inga marknadssuffix (.ST/.L/.DE osv)."""
    if not s:
        return ""
    s = s.strip().upper().replace(" ", "").replace("-", "")
    for suffix in (".ST", ".L", ".DE", ".US", ".OL", ".CO", ".HE", ".T", ".SS", ".SZ", ".HK", ".AS"):
        if s.endswith(suffix.replace(".", "")):
            s = s[: -len(suffix.replace(".", ""))]
    return s


def find_holding_match(entry_ticker: str, entry_name: str, holdings: dict):
    """Matchar en watchlist-post mot inlästa innehav. Provar (i ordning):
    exakt ticker, normaliserad ticker (hanterar t.ex. 'ADDT B' vs 'ADDT-B.ST'),
    sedan skiplistat namn (case-insensitive)."""
    if entry_ticker in holdings:
        return holdings[entry_ticker]
    if entry_name in holdings:
        return holdings[entry_name]

    norm_entry_ticker = _normalize_ticker(entry_ticker.split(".")[0] if "." in entry_ticker else entry_ticker)
    # strip known suffixes fully (handles multi-part like .ST)
    base_ticker = entry_ticker
    for suffix in (".ST", ".L", ".DE", ".US", ".OL", ".CO", ".HE", ".T", ".SS", ".SZ", ".HK", ".AS"):
        if base_ticker.endswith(suffix):
            base_ticker = base_ticker[: -len(suffix)]
            break
    norm_entry_ticker = _normalize_ticker(base_ticker)
    norm_entry_name = entry_name.strip().casefold() if entry_name else ""

    for hd in holdings.values():
        raw_ticker = hd.get("raw_ticker") or ""
        raw_name = hd.get("raw_name") or ""
        if norm_entry_ticker and _normalize_ticker(raw_ticker) == norm_entry_ticker:
            return hd
        if norm_entry_name and raw_name.strip().casefold() == norm_entry_name:
            return hd
    return None


def score_sell_signal(d):
    """Poäng (0-100) för säljvarning på ett befintligt innehav. Högre = starkare säljsignal."""
    score = 0
    reasons = []

    if d["rsi14"] is not None and d["rsi14"] > 70:
        score += 30
        reasons.append(f"RSI överköpt ({d['rsi14']})")

    if d["cross_signal"] == "death_cross":
        score += 35
        reasons.append("Death cross (SMA50 under SMA200)")

    if d["above_sma50"] is False:
        score += 15
        reasons.append("Pris under SMA50")

    if d["pe"] is not None and d["pe"] > 40:
        score += 15
        reasons.append(f"Högt P/E ({d['pe']}) – dyrt relativt vinst")

    if d["volume_ratio"] and d["volume_ratio"] > 2 and d["above_sma50"] is False:
        score += 15
        reasons.append(f"Förhöjd säljvolym ({d['volume_ratio']}x snitt) i nedgång")

    rec = d.get("recommendation_key")
    if rec == "strong_sell":
        score += 25
        reasons.append(f"Analytikerkonsensus: starkt sälj ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "sell":
        score += 15
        reasons.append(f"Analytikerkonsensus: sälj ({d.get('num_analysts') or '?'} analytiker)")
    elif rec == "strong_buy":
        score -= 20
        reasons.append(f"Analytikerkonsensus: starkt köp ({d.get('num_analysts') or '?'} analytiker) — talar tydligt emot att sälja")
    elif rec == "buy":
        score -= 8
        reasons.append(f"Analytikerkonsensus: köp ({d.get('num_analysts') or '?'} analytiker) — talar emot att sälja")

    upside = d.get("analyst_upside_pct")
    if upside is not None:
        if upside < -10:
            score += 15
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% under dagens pris")
        elif upside > 20:
            score -= 10
            reasons.append(f"Analytikernas kursmål {upside:+.0f}% över dagens pris — talar emot att sälja nu")

    if d.get("debt_to_equity") is not None and d["debt_to_equity"] > 150:
        score += 10
        reasons.append(f"Hög skuldsättning (D/E {d['debt_to_equity']})")

    if d.get("pb") is not None and d["pb"] > 8:
        score += 10
        reasons.append(f"Mycket högt P/B ({d['pb']})")

    if d.get("pb") is not None and d["pb"] < 0:
        score += 25
        reasons.append(f"Negativt P/B ({d['pb']}) – bolaget har negativt eget kapital, allvarlig varningssignal")

    if d.get("market_cap") is not None and d["market_cap"] < 50_000_000:
        score += 15
        reasons.append("Mikro-cap (<50M i börsvärde) – hög risk, tunn handel gör tekniska signaler opålitliga")

    if d.get("avg_dollar_volume") is not None and d["avg_dollar_volume"] < 100_000:
        score += 10
        reasons.append("Extremt låg likviditet (<100k i daglig omsättning) – svårt att komma ur positionen utan att flytta kursen")

    if d.get("volatility_pct") is not None and d["volatility_pct"] > 80:
        score += 10
        reasons.append(f"Mycket hög volatilitet ({d['volatility_pct']}% årstakt) – ovanligt stora kurssvängningar")

    return max(0, min(100, score)), reasons


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


def main():
    watchlist = load_watchlist()
    holdings = load_holdings()
    sector_weights, sector_factor_names = load_risk_factors()
    previously_held = load_previously_held()
    score_history = load_score_history()
    today_str = datetime.now(timezone.utc).date().isoformat()

    results = []
    current_holding_entries = []
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

        d["market"] = entry["market"]
        d["watchlist_name"] = entry["name"]
        d["sector"] = entry.get("sector")
        d["country"] = entry.get("country")

        hd = find_holding_match(ticker, entry["name"], holdings)
        is_holding = hd is not None
        d["is_holding"] = is_holding
        if is_holding:
            d["quantity"] = hd.get("quantity")
            current_holding_entries.append({"ticker": ticker, "name": entry["name"], "sector": entry.get("sector")})

        buy_score, buy_reasons = score_buy_candidate(d)
        sell_score, sell_reasons = (None, [])
        if is_holding:
            sell_score, sell_reasons = score_sell_signal(d)

        was_previously_held = (not is_holding) and ticker in previously_held
        d["previously_held"] = was_previously_held
        if was_previously_held:
            # Rent informativt — påverkar INTE köppoängen. Att du ägt en
            # aktie förut säger inget om att den är köpvärd nu.
            last_held = previously_held[ticker].get("last_held", "?")
            buy_reasons.append(f"Tidigare ägd av dig (senast {last_held}) – återköpskandidat (påverkar inte poängen)")

        sector = entry.get("sector")
        sector_weight = sector_weights.get(sector, 0) if sector else 0
        if sector_weight:
            buy_score = max(0, min(100, buy_score + sector_weight))
            names = ", ".join(sector_factor_names.get(sector, []))
            buy_reasons.append(f"Geopolitik/makro ({sector}): {names}")
            if is_holding:
                sell_score = max(0, min(100, sell_score - sector_weight))
                sell_reasons.append(f"Geopolitik/makro ({sector}): {names}")

        d["buy_score"] = buy_score
        d["buy_reasons"] = buy_reasons
        d["buy_days_on_list"], d["buy_score_delta"] = update_score_history(
            score_history, ticker, "buy", buy_score, today_str
        )
        if is_holding:
            d["sell_score"] = sell_score
            d["sell_reasons"] = sell_reasons
            d["sell_days_on_list"], d["sell_score_delta"] = update_score_history(
                score_history, ticker, "sell", sell_score, today_str
            )

        results.append(d)
        time.sleep(0.3)  # snäll mot Yahoo Finance

    updated_positions, did_update = update_previously_held(previously_held, current_holding_entries, len(holdings))
    if did_update:
        save_previously_held(updated_positions)
        print(f"Innehavshistorik uppdaterad ({len(updated_positions)} tickers totalt).", file=sys.stderr)
    else:
        print("Inga innehav laddade denna körning — innehavshistorik lämnas orörd.", file=sys.stderr)

    save_score_history(score_history)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": get_app_version(),
        "count": len(results),
        "holdings_loaded": len(holdings),
        "results": results,
    }

    DOCS_DIR.mkdir(exist_ok=True)
    output = sanitize_for_json(output)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Klar: {len(results)} tickers analyserade, skrivet till {OUTPUT_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
