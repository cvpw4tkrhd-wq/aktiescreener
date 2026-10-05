"""Signaluppföljning (v10.0): hur har appens poäng fungerat?

1. Uppföljning av köpsignaler: för varje datum i data/score_history.json delas
   aktierna in efter köppoäng (KÖP ≥65, 50–64, under 50) och avkastningen efter
   1 vecka, 1, 3 och 6 månader mäts mot snittet för alla bevakade aktier samma
   datum. Historiken är ännu kort – siffrorna blir pålitligare med tiden.
2. Historiskt test av trenddelen: Yahoo saknar historiska nyckeltal, men
   trenddelen (SMA50/SMA200 och RSI) kan räknas fram bakåt ur kurserna. Testet
   görs vid varje månadsskifte de senaste två åren.

Skriver docs/followup.json. Påverkar inte poängen.
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "score_history.json"
RESULTS_FILE = ROOT / "docs" / "results.json"
OUT_FILE = ROOT / "docs" / "followup.json"
VERSION_FILE = ROOT / "VERSION"
HORIZONS = [(5, "1 vecka"), (21, "1 månad"), (63, "3 månader"), (126, "6 månader")]


def bucket_live(score):
    return "buy" if score >= 65 else ("mid" if score >= 50 else "low")


def rsi(series, n=14):
    delta = series.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, float("nan")))


def summarize(rows):
    """rows: lista av (bucket, överavkastning mot snittet i %, slog medianen samma datum).
    Träffsäkerhet = andel som slog medianen (50 % = ingen förmåga)."""
    out = {}
    for b in sorted({r[0] for r in rows}):
        xs = [r for r in rows if r[0] == b and r[1] is not None and not math.isnan(r[1])]
        if xs:
            out[b] = {"n": len(xs), "avg_excess": round(sum(x[1] for x in xs) / len(xs), 2),
                      "hit_rate": round(sum(1 for x in xs if x[2]) / len(xs) * 100, 1)}
    return out


def main():
    hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    tickers = sorted(t for t, v in hist.items() if isinstance(v, dict) and v.get("buy"))
    try:
        res = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
        current = {r["ticker"] for r in res.get("results", [])}
        tickers = [t for t in tickers if t in current]
    except Exception:
        current = set(tickers)
    print(f"Signaluppföljning: hämtar kurser för {len(current)} aktier", file=sys.stderr)
    px = yf.download(sorted(current), period="3y", auto_adjust=True, progress=False, threads=True)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame()
    px = px.dropna(how="all").ffill()
    px.index = pd.to_datetime(px.index).tz_localize(None)
    dates_idx = px.index

    def pos_on_or_before(d):
        i = dates_idx.searchsorted(pd.Timestamp(d), side="right") - 1
        return i if i >= 0 else None

    # ---------- 1. Uppföljning av appens köpsignaler ----------
    obs = {}   # (iso-vecka, ticker) -> (datum, poäng): en observation per aktie och vecka
    for t in tickers:
        for e in hist[t]["buy"]:
            d = pd.Timestamp(e["date"])
            key = (d.isocalendar()[0], d.isocalendar()[1], t)
            if key not in obs:
                obs[key] = (d, e["score"])
    live = {"first_date": None, "last_date": None, "observations": len(obs), "horizons": []}
    if obs:
        ds = sorted(v[0] for v in obs.values())
        live["first_date"], live["last_date"] = ds[0].date().isoformat(), ds[-1].date().isoformat()
    for h, label in HORIZONS:
        per_date = {}
        for (_, _, t), (d, sc) in obs.items():
            if t not in px.columns:
                continue
            i = pos_on_or_before(d)
            if i is None or i + h >= len(dates_idx):
                continue
            p0, p1 = px[t].iloc[i], px[t].iloc[i + h]
            if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                continue
            per_date.setdefault(i, []).append((bucket_live(sc), (p1 / p0 - 1) * 100))
        rows = []
        for i, lst in per_date.items():
            if len(lst) < 10:
                continue
            avg = sum(x[1] for x in lst) / len(lst)
            med = sorted(x[1] for x in lst)[len(lst) // 2]
            rows += [(b, r - avg, r > med) for b, r in lst]
        live["horizons"].append({"days": h, "label": label, "dates": len([1 for l in per_date.values() if len(l) >= 10]),
                                 "buckets": summarize(rows)})

    # ---------- 2. Historiskt test av trenddelen ----------
    bt = {"months": 0, "first": None, "last": None, "horizons": []}
    sma50, sma200, r14 = px.rolling(50).mean(), px.rolling(200).mean(), px.apply(rsi)
    month_ends = [d for d in pd.Series(dates_idx, index=dates_idx).groupby(dates_idx.to_period("M")).max()]
    month_ends = [d for d in month_ends if dates_idx.get_loc(d) >= 200]
    for h, label in HORIZONS[1:3]:
        rows = {"combined": [], "trend": [], "rsi": []}
        used = 0
        for d in month_ends:
            i = dates_idx.get_loc(d)
            if i + h >= len(dates_idx):
                continue
            p0, p1 = px.iloc[i], px.iloc[i + h]
            ret = (p1 / p0 - 1) * 100
            ok = ret.notna() & sma200.iloc[i].notna()
            if ok.sum() < 20:
                continue
            used += 1
            avg, med = ret[ok].mean(), ret[ok].median()
            for t in ret[ok].index:
                pr, s50, s200, rs = p0[t], sma50.iloc[i][t], sma200.iloc[i][t], r14.iloc[i][t]
                tr = 8 if (pr > s50 and pr > s200) else (-10 if (pr < s50 and pr < s200) else 0)
                ri = (10 if rs < 35 else (-10 if rs > 70 else 0)) if rs == rs else 0
                ex, hit = ret[t] - avg, ret[t] > med
                lab = lambda sc: "pos" if sc > 0 else ("neg" if sc < 0 else "neu")
                rows["combined"].append((lab(tr + ri), ex, hit))
                rows["trend"].append((lab(tr), ex, hit))
                rows["rsi"].append((lab(ri), ex, hit))
        if used:
            bt["months"] = max(bt["months"], used)
        bt["horizons"].append({"days": h, "label": label, "months": used,
                               "buckets": summarize(rows["combined"]),
                               "trend_only": summarize(rows["trend"]), "rsi_only": summarize(rows["rsi"])})
    if month_ends:
        bt["first"], bt["last"] = month_ends[0].date().isoformat(), month_ends[-1].date().isoformat()

    version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else None
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": version,
           "stocks": len(current), "live": live, "trend_backtest": bt}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Signaluppföljning skriven: {len(obs)} veckoobservationer, trendtest {bt['months']} månader", file=sys.stderr)


if __name__ == "__main__":
    main()
