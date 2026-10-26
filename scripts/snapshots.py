"""Månadssnapshots av poängsatta aktier och fonder (v11.8).

Vid första körningen varje månad (den 1:a, eller första vardagen därefter – workflowen
körs bara vardagar) sparas en snapshot i data/snapshots/ÅÅÅÅ-MM.json: poäng, pris,
rekommendation och några nyckelfält för alla aktier och fonder, plus makroläget.

Varje körning bygger sedan docs/snapshots.json med jämförelser:
  • live:  senaste snapshot → idag
  • varje par av efterföljande snapshots (sparas i data/snapshots/compare_*.json så
    att kursdata inte behöver hämtas om)
  • första snapshot → idag när det finns minst tre

Jämförelsen visar poängförändringar, vilka som kom in i eller lämnade KÖP, samt hur
grupperna (efter poäng vid starten) har utvecklats i kurs (justerad för utdelningar och
splittar). Påverkar inte poängen.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(os.environ.get("SNAP_ROOT") or Path(__file__).resolve().parent.parent)
RESULTS_FILE = ROOT / "docs" / "results.json"
FUNDS_FILE = ROOT / "docs" / "funds.json"
SNAP_DIR = ROOT / "data" / "snapshots"
OUT_FILE = ROOT / "docs" / "snapshots.json"
VERSION_FILE = ROOT / "VERSION"
BUY_THRESHOLD = 75   # samma som KÖP-gränsen i screener.py / index.html


# ---------------------------------------------------------------- snapshots
def build_snapshot(results, funds):
    gen = results.get("generated_at") or datetime.now(timezone.utc).isoformat()
    bonds = (results.get("macro") or {}).get("bonds") or {}
    stab = bonds.get("stability") or {}
    y10 = next((y.get("value") for y in bonds.get("yields", []) if y.get("label") == "10Y"), None)
    stocks = {}
    for r in results.get("results", []):
        stocks[r["ticker"]] = {
            "n": r.get("watchlist_name") or r.get("name") or r["ticker"], "s": r.get("sector"), "m": r.get("market"),
            "b": r.get("buy_score"), "g": r.get("growth_score"), "gc": bool(r.get("growth_candidate")),
            "p": r.get("price"), "c": r.get("currency"), "r": r.get("recommendation_key"),
            "v": (r.get("valuation") or {}).get("verdict"), "it": r.get("investtech_rank"),
        }
    fnds = {}
    for f in (funds or {}).get("funds", []):
        fnds[f["ticker"]] = {"n": f.get("name"), "c": f.get("category"), "s": f.get("score"), "l": f.get("label"),
                             "p": f.get("price"), "y1": f.get("ret_1y")}
    return {
        "month": gen[:7], "date": gen[:10], "taken_at": gen, "version": results.get("version"),
        "macro": {"stability": stab.get("score"), "label": stab.get("label"), "treasury_10y": y10,
                  "adjustment": (results.get("macro") or {}).get("score_adjustment")},
        "stocks": stocks, "funds": fnds,
    }


def load_snapshots():
    snaps = []
    for p in sorted(SNAP_DIR.glob("20??-??.json")):
        try:
            snaps.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            print(f"  Kunde inte läsa {p.name}: {e}", file=sys.stderr)
    return snaps


def maybe_take_snapshot(results, funds, snaps):
    snap = build_snapshot(results, funds)
    if any(s["month"] == snap["month"] for s in snaps):
        return None
    snap["kind"] = "start" if not snaps else "monthly"
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    (SNAP_DIR / f"{snap['month']}.json").write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    print(f"Snapshot {snap['month']} sparad ({len(snap['stocks'])} aktier, {len(snap['funds'])} fonder)", file=sys.stderr)
    return snap


# ---------------------------------------------------------------- kurser
def price_returns(tickers, d_from, d_to):
    """Total avkastning i % mellan två datum (justerade kurser). d_to=None betyder senaste."""
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    start = pd.Timestamp(d_from) - pd.Timedelta(days=8)
    end = (pd.Timestamp(d_to) if d_to else pd.Timestamp.today()) + pd.Timedelta(days=3)
    try:
        px = yf.download(tickers, start=str(start.date()), end=str(end.date()), auto_adjust=True, progress=False,
                         threads=True)["Close"]
    except Exception as e:
        print(f"  Kursdata kunde inte hämtas: {e}", file=sys.stderr)
        return {}
    if isinstance(px, pd.Series):
        px = px.to_frame(tickers[0])
    px = px.dropna(how="all").ffill()
    if px.empty:
        return {}
    px.index = pd.to_datetime(px.index).tz_localize(None)
    a_rows = px.loc[: pd.Timestamp(d_from)]
    if a_rows.empty:
        return {}
    a = a_rows.iloc[-1]
    b = (px.loc[: pd.Timestamp(d_to)] if d_to else px).iloc[-1]
    out = {}
    for t in px.columns:
        if pd.notna(a.get(t)) and pd.notna(b.get(t)) and a[t] > 0:
            out[t] = round(float((b[t] / a[t] - 1) * 100), 2)
    return out


# ---------------------------------------------------------------- jämförelse
def _median(xs):
    xs = sorted(xs)
    return None if not xs else (xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2)


def _bucket_stats(rows, key_fn, order):
    """Medelavkastning per grupp (grupp efter poäng vid starten)."""
    allret = [r["ret"] for r in rows if r["ret"] is not None]
    uni_avg = sum(allret) / len(allret) if allret else None
    uni_med = _median(allret)
    out = []
    for name in order:
        g = [r["ret"] for r in rows if key_fn(r) == name and r["ret"] is not None]
        if not g:
            out.append({"group": name, "n": 0})
            continue
        out.append({"group": name, "n": len(g), "avg": round(sum(g) / len(g), 2), "median": round(_median(g), 2),
                    "excess": round(sum(g) / len(g) - uni_avg, 2) if uni_avg is not None else None,
                    "beat_median": round(100 * sum(1 for x in g if x > uni_med) / len(g), 0)})
    return out, (round(uni_avg, 2) if uni_avg is not None else None)


def _ic(rows, a_key="a"):
    xs = [(r[a_key], r["ret"]) for r in rows if r.get(a_key) is not None and r["ret"] is not None]
    if len(xs) < 20:
        return None
    s = pd.Series([x[0] for x in xs]).rank()
    t = pd.Series([x[1] for x in xs]).rank()
    c = s.corr(t)
    return None if pd.isna(c) else round(float(c), 3)


def compare(A, B, ret_stocks, ret_funds, comp_id):
    """A och B är snapshots (B kan vara 'live', byggd av dagens data)."""
    days = (datetime.fromisoformat(B["date"]) - datetime.fromisoformat(A["date"])).days
    out = {"id": comp_id, "from": A["month"], "to": B["month"] if comp_id != "live" else "live",
           "from_date": A["date"], "to_date": B["date"], "days": days,
           "from_version": A.get("version"), "to_version": B.get("version"),
           "model_changed": A.get("version") != B.get("version")}
    # ---- aktier
    rows = []
    for t, a in A["stocks"].items():
        b = B["stocks"].get(t)
        if not b or a.get("b") is None or b.get("b") is None:
            continue
        rows.append({"t": t, "n": b.get("n"), "a": a["b"], "b": b["b"], "d": b["b"] - a["b"], "ret": ret_stocks.get(t)})
    ds = [r["d"] for r in rows]
    bucket = lambda r: "KÖP" if r["a"] >= BUY_THRESHOLD else ("MELLAN" if r["a"] >= 50 else "LÅG")
    groups, uni = _bucket_stats(rows, bucket, ["KÖP", "MELLAN", "LÅG"])
    entered = [r for r in rows if r["a"] < BUY_THRESHOLD <= r["b"]]
    left = [r for r in rows if r["a"] >= BUY_THRESHOLD > r["b"]]
    slim = lambda r: {k: r[k] for k in ("t", "n", "a", "b", "ret")}
    out["stocks"] = {
        "n": len(rows), "new": len([t for t in B["stocks"] if t not in A["stocks"]]),
        "removed": len([t for t in A["stocks"] if t not in B["stocks"]]),
        "avg_change": round(sum(ds) / len(ds), 2) if ds else None, "up": sum(1 for d in ds if d > 0), "down": sum(1 for d in ds if d < 0),
        "buy_a": sum(1 for r in rows if r["a"] >= BUY_THRESHOLD), "buy_b": sum(1 for r in rows if r["b"] >= BUY_THRESHOLD),
        "groups": groups, "universe_avg_return": uni, "ic": _ic(rows),
        "entered": [slim(r) for r in sorted(entered, key=lambda r: -r["b"])[:12]],
        "left": [slim(r) for r in sorted(left, key=lambda r: r["b"])[:12]],
        "top_up": [slim(r) for r in sorted([r for r in rows if r["d"] > 0], key=lambda r: -r["d"])[:8]],
        "top_down": [slim(r) for r in sorted([r for r in rows if r["d"] < 0], key=lambda r: r["d"])[:8]],
    }
    # ---- fonder
    frows = []
    for t, a in A["funds"].items():
        b = B["funds"].get(t)
        if not b or a.get("s") is None or b.get("s") is None:
            continue
        frows.append({"t": t, "n": b.get("n"), "c": b.get("c"), "a": a["s"], "b": b["s"], "d": b["s"] - a["s"],
                      "la": a.get("l"), "lb": b.get("l"), "ret": ret_funds.get(t)})
    fd = [r["d"] for r in frows]
    labels = [l for l in ("Lovande", "Neutral", "Svag") if any(r["la"] == l for r in frows)]
    fgroups, funi = _bucket_stats(frows, lambda r: r["la"], labels)
    fslim = lambda r: {k: r[k] for k in ("t", "n", "c", "a", "b", "la", "lb", "ret")}
    out["funds"] = {
        "n": len(frows), "new": len([t for t in B["funds"] if t not in A["funds"]]),
        "avg_change": round(sum(fd) / len(fd), 2) if fd else None, "up": sum(1 for d in fd if d > 0), "down": sum(1 for d in fd if d < 0),
        "groups": fgroups, "universe_avg_return": funi, "ic": _ic(frows),
        "top_up": [fslim(r) for r in sorted([r for r in frows if r["d"] > 0], key=lambda r: -r["d"])[:5]],
        "top_down": [fslim(r) for r in sorted([r for r in frows if r["d"] < 0], key=lambda r: r["d"])[:5]],
        "label_changes": [fslim(r) for r in frows if r["la"] != r["lb"]][:12],
    }
    out["macro"] = {"stability_a": (A.get("macro") or {}).get("stability"), "stability_b": (B.get("macro") or {}).get("stability"),
                    "label_a": (A.get("macro") or {}).get("label"), "label_b": (B.get("macro") or {}).get("label"),
                    "y10_a": (A.get("macro") or {}).get("treasury_10y"), "y10_b": (B.get("macro") or {}).get("treasury_10y")}
    return out


def build_comparison(A, B, comp_id, to_date_for_prices):
    tickers_s = [t for t in A["stocks"] if t in B["stocks"]]
    tickers_f = [t for t in A["funds"] if t in B["funds"]]
    ret = {}
    if (datetime.fromisoformat(B["date"]) - datetime.fromisoformat(A["date"])).days >= 1:
        ret = price_returns(tickers_s + tickers_f, A["date"], to_date_for_prices)
    return compare(A, B, {t: ret.get(t) for t in tickers_s}, {t: ret.get(t) for t in tickers_f}, comp_id)


def main():
    try:
        results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Snapshots: results.json saknas ({e})", file=sys.stderr)
        return
    try:
        funds = json.loads(FUNDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        funds = {}
    snaps = load_snapshots()
    new = maybe_take_snapshot(results, funds, snaps)
    if new:
        snaps.append(new)
        snaps.sort(key=lambda s: s["month"])
    if not snaps:
        return
    live = build_snapshot(results, funds)
    comps = []
    # live: senaste snapshot → idag
    last = snaps[-1]
    comps.append(build_comparison(last, live, "live", None))
    # efterföljande par (cachas)
    for a, b in zip(snaps[:-1], snaps[1:]):
        cid = f"{a['month']}_{b['month']}"
        cache = SNAP_DIR / f"compare_{cid}.json"
        if cache.exists():
            try:
                comps.append(json.loads(cache.read_text(encoding="utf-8")))
                continue
            except Exception:
                pass
        c = build_comparison(a, b, cid, b["date"])
        cache.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        comps.append(c)
    # första snapshot → idag när det finns minst tre
    if len(snaps) >= 3:
        comps.append(build_comparison(snaps[0], live, "first_live", None))
    special = [c for c in comps if c["id"] in ("live", "first_live")]
    special.sort(key=lambda c: 0 if c["id"] == "live" else 1)
    pairs = sorted([c for c in comps if c["id"] not in ("live", "first_live")], key=lambda c: c["id"], reverse=True)
    comps = special + pairs
    version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else None
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "version": version, "buy_threshold": BUY_THRESHOLD,
           "snapshots": [{"month": s["month"], "date": s["date"], "version": s.get("version"), "kind": s.get("kind"),
                          "stocks": len(s["stocks"]), "funds": len(s["funds"]),
                          "stability": (s.get("macro") or {}).get("stability"), "label": (s.get("macro") or {}).get("label")} for s in snaps],
           "comparisons": comps}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"Snapshots: {len(snaps)} snapshot(s), {len(comps)} jämförelse(r) skrivna till {OUT_FILE.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
