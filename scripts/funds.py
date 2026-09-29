"""Fondscreener (v8.7): hämtar kurshistorik för fonderna i data/funds.yml,
räknar trend-, momentum-, risk-, avgifts- och makropoäng (0-100) och skriver
docs/funds.json. Körs efter screener.py i samma workflow och läser därifrån
makroläge (stabilitetspoäng) och marknadsbredd ur docs/results.json.
Påverkar inte aktiedatan."""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
FUNDS_FILE = ROOT / "data" / "funds.yml"
RF_FILE = ROOT / "data" / "risk_free_rates.json"
RESULTS_FILE = ROOT / "docs" / "results.json"
OUT_FILE = ROOT / "docs" / "funds.json"
VERSION_FILE = ROOT / "VERSION"

CCY_RF = {"SEK": "SE", "EUR": "DE", "USD": "US", "NOK": "NO", "DKK": "DK", "GBP": "GB"}


def _clean(v, d=2):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, d)


def _ret(close, days):
    if len(close) <= days:
        return None
    return (close.iloc[-1] / close.iloc[-1 - days] - 1) * 100


def _rsi(close, n=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, float("nan"))
    return float((100 - 100 / (1 + rs)).iloc[-1])


def fetch_fund(cfg, rf_rates):
    """rf_rates: kort riskfri ränta per valuta (v8.8, tidigare 10-årsränta per land)."""
    tk = yf.Ticker(cfg["ticker"])
    h = tk.history(period="2y", auto_adjust=True)
    close = h["Close"].dropna() if len(h) else pd.Series(dtype=float)
    if len(close) < 120:
        print(f"  {cfg['ticker']}: för lite historik ({len(close)})", file=sys.stderr)
        return None
    if getattr(close.index, "tz", None) is not None:
        close.index = close.index.tz_localize(None)
    try:
        info = tk.get_info() or {}
    except Exception:
        info = {}
    ccy = info.get("currency") or ""
    price = float(close.iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    daily = close.pct_change().dropna()
    last_year = close[close.index >= close.index[-1] - pd.Timedelta(days=365)]
    vol = float(daily.tail(252).std() * math.sqrt(252) * 100)
    dd = float(((last_year / last_year.cummax()) - 1).min() * 100)
    ret_1y = _ret(close, 252)
    rf = rf_rates.get(ccy, rf_rates.get("SEK", 2.0))
    sharpe = (ret_1y - rf) / vol if ret_1y is not None and vol > 0 else None
    ytd_start = close[close.index < pd.Timestamp(close.index[-1].year, 1, 1)]
    ret_ytd = (price / float(ytd_start.iloc[-1]) - 1) * 100 if len(ytd_start) else None
    sma200_series = close.rolling(200).mean()
    sma200_slope = None
    if len(sma200_series.dropna()) > 21:
        sma200_slope = (float(sma200_series.iloc[-1]) / float(sma200_series.iloc[-22]) - 1) * 100
    cross = None
    if sma200:
        cross = "golden" if sma50 > sma200 else "death"
    fee = cfg.get("fee_pct")
    fee_src = "Avanza (2026-09)" if fee is not None else None
    if fee is None:
        ner, arer = info.get("netExpenseRatio"), info.get("annualReportExpenseRatio")
        if isinstance(ner, (int, float)) and 0 < ner < 5:
            fee, fee_src = float(ner), "Yahoo Finance"
        elif isinstance(arer, (int, float)) and 0 < arer < 0.05:
            fee, fee_src = float(arer) * 100, "Yahoo Finance"
    step = max(1, len(last_year) // 180)
    return {
        "name": cfg["name"], "ticker": cfg["ticker"], "isin": cfg.get("isin"),
        "category": cfg.get("category"), "asset_class": cfg.get("asset_class", "equity"),
        "proxy": bool(cfg.get("proxy")), "proxy_note": cfg.get("proxy_note"),
        "currency": ccy, "price": _clean(price), "as_of": str(close.index[-1].date()),
        "sma50": _clean(sma50), "sma200": _clean(sma200),
        "above_sma50": price > sma50, "above_sma200": (price > sma200) if sma200 else None,
        "sma50_vs_200_pct": _clean((sma50 / sma200 - 1) * 100, 1) if sma200 else None,
        "cross": cross, "rsi14": _clean(_rsi(close), 1),
        "sma200_dist_pct": _clean((price / sma200 - 1) * 100, 1) if sma200 else None,
        "sma200_slope_pct": _clean(sma200_slope, 2),
        "ret_1m": _clean(_ret(close, 21), 1), "ret_3m": _clean(_ret(close, 63), 1),
        "ret_6m": _clean(_ret(close, 126), 1), "ret_1y": _clean(ret_1y, 1), "ret_ytd": _clean(ret_ytd, 1),
        "volatility_pct": _clean(vol, 1), "max_drawdown_pct": _clean(dd, 1), "sharpe": _clean(sharpe, 2),
        "fee_pct": _clean(fee, 2), "fee_source": fee_src,
        "price_history_1y": [_clean(v) for v in last_year.iloc[::step].tolist()],
    }


def _lin(x, x0, x1, y0, y1):
    if x is None:
        return None
    if x <= x0:
        return y0
    if x >= x1:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def region_of(cat):
    c = (cat or "").lower()
    if c.startswith("sverige") or "norden" in c:
        return "nordic"
    if c.startswith("usa"):
        return "us"
    if c.startswith("europa"):
        return "eu"
    return "all"


def macro_context():
    try:
        d = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None, {}
    stab = (((d.get("macro") or {}).get("bonds") or {}).get("stability") or {}).get("score")
    groups = {"us": [], "eu": [], "nordic": [], "all": []}
    for r in d.get("results", []):
        v = r.get("above_sma200")
        if v is None:
            continue
        m = r.get("market")
        g = "us" if m == "US" else ("nordic" if m in ("SE", "NO", "DK", "FI") else "eu")
        groups[g].append(bool(v)); groups["all"].append(bool(v))
    breadth = {k: (sum(v) / len(v) * 100 if v else None) for k, v in groups.items()}
    return stab, breadth


def score_funds(funds, stab, breadth):
    # Momentum: blandad avkastning rankad inom jämförbar klass
    def blend(f):
        parts = [(f.get("ret_3m"), .3), (f.get("ret_6m"), .3), (f.get("ret_1y"), .4)]
        num = sum(v * w for v, w in parts if v is not None)
        den = sum(w for v, w in parts if v is not None)
        return num / den if den else None
    def rank_into(groups, key):
        for lst in groups.values():
            ranked = sorted([f for f in lst if blend(f) is not None], key=blend, reverse=True)
            n = len(ranked)
            for i, f in enumerate(ranked):
                f[key] = (1 - i / (n - 1) if n > 1 else 0.5, i + 1, n)
    classes, cats = {}, {}
    for f in funds:
        classes.setdefault(f["asset_class"] if f["asset_class"] == "bond" else "equity_like", []).append(f)
        cats.setdefault(f.get("category") or "?", []).append(f)
    rank_into(classes, "_r_cls")
    rank_into({k: v for k, v in cats.items() if len(v) >= 3}, "_r_cat")
    # v8.8: hälften mot fonder i samma kategori (minst 3 st), hälften mot alla
    # jämförbara – annars vinner bara den marknad som är hetast just nu.
    for f in funds:
        rc, rk = f.get("_r_cls"), f.get("_r_cat")
        if rc is None:
            continue
        f["_mom_pct"] = (rc[0] + rk[0]) / 2 if rk else rc[0]
        f["momentum_rank"] = f"{rk[1]} av {rk[2]} i kategorin" if rk else f"{rc[1]} av {rc[2]}"

    for f in funds:
        pos, neg = [], []
        # Trend (35, v8.8): graderad i stället för ja/nej
        t = 0
        dist, slope = f.get("sma200_dist_pct"), f.get("sma200_slope_pct")
        if dist is not None:
            t += _lin(dist, -5, 15, 0, 14)
            if dist > 0:
                pos.append(f"Kursen {dist:.1f} % över SMA200 – långsiktig upptrend")
            else:
                neg.append(f"Kursen {abs(dist):.1f} % under SMA200 – långsiktig nedtrend")
        if slope is not None:
            t += _lin(slope, -2, 3, 0, 8)
            if slope > 0.5:
                pos.append(f"SMA200 stiger ({slope:+.1f} % senaste månaden)")
            elif slope < -0.5:
                neg.append(f"SMA200 faller ({slope:+.1f} % senaste månaden)")
        if f["above_sma50"]:
            t += 6; pos.append("Kursen ligger över SMA50 – kortsiktig styrka")
        else:
            neg.append("Kursen ligger under SMA50 – kortsiktig svaghet")
        if f["cross"] == "golden":
            t += 3
        elif f["cross"] == "death":
            neg.append("SMA50 under SMA200 (death cross-läge)")
        rsi = f.get("rsi14")
        if rsi is not None:
            if 40 <= rsi <= 70:
                t += 4
            elif rsi > 75:
                neg.append(f"RSI {rsi:.0f} – kortsiktigt överköpt")
            elif rsi < 30:
                pos.append(f"RSI {rsi:.0f} – kortsiktigt översåld, kan vara läge att öka")
                t += 2
        # Momentum (25)
        mp = f.get("_mom_pct")
        m = 25 * mp if mp is not None else 12.5
        if mp is not None and mp >= 0.7:
            pos.append(f"Stark avkastning jämfört med andra fonder (plats {f['momentum_rank']})")
        elif mp is not None and mp <= 0.3:
            neg.append(f"Svag avkastning jämfört med andra fonder (plats {f['momentum_rank']})")
        # Riskjusterad avkastning (25)
        sh, dd = f.get("sharpe"), f.get("max_drawdown_pct")
        r_sh = _lin(sh, 0, 2.5, 0, 17) if sh is not None else 8.5
        r_dd = _lin(-dd, 10, 30, 8, 0) if dd is not None else 4
        if f["asset_class"] == "bond":
            r_dd = _lin(-dd, 2, 8, 8, 0) if dd is not None else 4
        r = r_sh + r_dd
        if sh is not None and sh >= 1:
            pos.append(f"Bra riskjusterad avkastning (Sharpe {sh:.2f})")
        elif sh is not None and sh < 0:
            neg.append(f"Sämre än riskfri ränta senaste året (Sharpe {sh:.2f})")
        if dd is not None and ((f["asset_class"] != "bond" and dd < -25) or (f["asset_class"] == "bond" and dd < -6)):
            neg.append(f"Stort fall från toppen senaste året ({dd:.0f}%)")
        # Avgift (10)
        fee = f.get("fee_pct")
        if fee is None:
            c = 5
        else:
            c = 10 if fee <= 0.3 else (7 if fee <= 0.8 else (4 if fee <= 1.5 else 0))
            if fee <= 0.3:
                pos.append(f"Låg avgift ({fee:.2f}%)")
            elif fee > 1.5:
                neg.append(f"Hög avgift ({fee:.2f}%)")
        # Makro (10)
        if f["asset_class"] in ("bond", "realestate"):
            mac = stab / 10 if stab is not None else 5
            if stab is not None and stab >= 70:
                pos.append(f"Stabilt ränteläge ({stab:.0f}/100) gynnar räntekänsliga fonder")
            elif stab is not None and stab < 50:
                neg.append(f"Oroligt ränteläge ({stab:.0f}/100) – räntekänsliga fonder utsatta")
        elif f["asset_class"] == "commodity":
            mac = 5
        else:
            b = breadth.get(region_of(f.get("category")))
            if b is None:
                b = breadth.get("all")
            mac = b / 10 if b is not None else 5
            if b is not None and b >= 65:
                pos.append(f"Bred uppgång på marknaden ({b:.0f}% av aktierna över SMA200)")
            elif b is not None and b < 40:
                neg.append(f"Svag marknadsbredd ({b:.0f}% av aktierna över SMA200)")
        mac = max(0, min(10, mac)) / 2   # v8.8: makro väger 5 poäng
        total = round(t + m + r + c + mac)
        f["score"] = int(max(0, min(100, total)))
        f["score_parts"] = {"trend": round(t, 1), "momentum": round(m, 1), "risk": round(r, 1), "cost": round(c, 1), "macro": round(mac, 1)}
        for k in ("_r_cls", "_r_cat"):
            f.pop(k, None)
        f["label"] = "Lovande" if f["score"] >= 75 else ("Neutral" if f["score"] >= 50 else "Svag")
        f["reasons_pos"], f["reasons_neg"] = pos, neg
        f.pop("_mom_pct", None)
    return funds


def main():
    cfg = yaml.safe_load(FUNDS_FILE.read_text(encoding="utf-8")) or {}
    rf_rates = dict(cfg.get("short_rates") or {})
    try:
        ys = (json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("macro") or {}).get("bonds", {}).get("yields", [])
        m3 = next((y["value"] for y in ys if y.get("label") == "3M"), None)
        if m3 is not None:
            rf_rates["USD"] = m3
    except Exception:
        pass
    rf_rates.setdefault("USD", 4.0)
    funds = []
    for c in cfg.get("funds", []):
        try:
            f = fetch_fund(c, rf_rates)
            if f:
                funds.append(f)
        except Exception as e:
            print(f"  {c.get('ticker')}: {type(e).__name__}: {e}", file=sys.stderr)
    stab, breadth = macro_context()
    score_funds(funds, stab, breadth)
    version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else None
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": version,
           "count": len(funds), "stability_score": stab, "funds": funds}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Fonder: {len(funds)} av {len(cfg.get('funds', []))} skrivna till {OUT_FILE.name}")


if __name__ == "__main__":
    main()
