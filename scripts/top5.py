#!/usr/bin/env python3
"""
Top 5-strategin: hittar förra kalenderårets fem bästa aktier i S&P 500
(högst totalavkastning, alltså kursutveckling + utdelningar) och skriver
resultatet till docs/top5.json, som fliken TOP 5 i dashboarden läser.

Körs så här:
  python scripts/top5.py             # behåller årets lista, uppdaterar bara kurserna
                                     # (räknar om automatiskt när nytt år börjat)
  python scripts/top5.py --recompute # tvingar fram en ny rankning från grunden

Listan ska vara stabil under hela året: den räknas bara om när "köpåret"
(innevarande år) skiljer sig från det som redan ligger i top5.json.
"""
import argparse
import datetime as dt
import io
import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "top5.json"

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
FALLBACK_CSV = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500.csv"
UA = {"User-Agent": "Mozilla/5.0 (compatible; aktiescreener/1.0)"}
TOP_N = 5
MIN_COVERAGE = 0.90  # kräver kursdata för minst 90 % av bolagen, annars avbryts körningen


def get_universe() -> pd.DataFrame:
    """Aktuella S&P 500-bolag: kolumnerna symbol, name, sector."""
    try:
        import requests
        html = requests.get(WIKI_URL, headers=UA, timeout=30).text
        df = pd.read_html(io.StringIO(html))[0]
        df = df.rename(columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "sector"})
        print(f"Bolagslista från Wikipedia: {len(df)} bolag")
    except Exception as exc:  # noqa: BLE001
        print(f"Wikipedia misslyckades ({exc}), använder reservlistan")
        df = pd.read_csv(FALLBACK_CSV)
        df = df.rename(columns={"Symbol": "symbol", "Security": "name", "GICS Sector": "sector"})
        print(f"Bolagslista från reservkällan: {len(df)} bolag")
    df = df[["symbol", "name", "sector"]].dropna(subset=["symbol"]).copy()
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["yf"] = df["symbol"].str.replace(".", "-", regex=False)  # BRK.B -> BRK-B för Yahoo
    return df.drop_duplicates("yf").reset_index(drop=True)


def download_close(tickers, start, end) -> pd.DataFrame:
    data = yf.download(
        list(tickers), start=start, end=end,
        auto_adjust=True, progress=False, threads=True,
    )
    close = data["Close"] if "Close" in data else data
    if isinstance(close, pd.Series):
        close = close.to_frame(list(tickers)[0])
    return close


def last_on_or_before(series: pd.Series, day: str):
    s = series.loc[:day].dropna()
    return (float(s.iloc[-1]), s.index[-1]) if len(s) else (None, None)


def rank_year(ranking_year: int) -> tuple[list[dict], int, int]:
    uni = get_universe()
    start = f"{ranking_year - 1}-12-01"
    end = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    close = download_close(uni["yf"], start, end)

    rows = []
    for _, u in uni.iterrows():
        if u["yf"] not in close.columns:
            continue
        s = close[u["yf"]]
        base, base_dt = last_on_or_before(s, f"{ranking_year - 1}-12-31")
        endp, end_dt = last_on_or_before(s, f"{ranking_year}-12-31")
        if base is None or endp is None or base_dt.year != ranking_year - 1 or end_dt.year != ranking_year:
            continue
        latest = float(s.dropna().iloc[-1])
        rows.append({
            "ticker": u["symbol"], "name": u["name"], "sector": u["sector"],
            "ret_prev_year_pct": round((endp / base - 1) * 100, 1),
            "base_price": round(endp, 2),           # kurs vid årsskiftet (jämförelsepunkt för "hittills i år")
            "price": round(latest, 2),
            "ytd_pct": round((latest / endp - 1) * 100, 1),
        })
    coverage = len(rows) / max(len(uni), 1)
    print(f"Kursdata för {len(rows)} av {len(uni)} bolag ({coverage:.0%})")
    if coverage < MIN_COVERAGE:
        sys.exit("För låg täckning i kursdatan – avbryter så att en felaktig lista inte sparas.")
    rows.sort(key=lambda r: r["ret_prev_year_pct"], reverse=True)
    picks = rows[:TOP_N]
    for i, p in enumerate(picks, 1):
        p["rank"] = i
    return picks, len(uni), len(rows)


def refresh_prices(picks: list[dict]) -> str:
    yf_map = {p["ticker"]: p["ticker"].replace(".", "-") for p in picks}
    start = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    end = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    close = download_close(yf_map.values(), start, end)
    last_day = None
    for p in picks:
        col = yf_map[p["ticker"]]
        if col not in close.columns or close[col].dropna().empty:
            continue
        s = close[col].dropna()
        p["price"] = round(float(s.iloc[-1]), 2)
        p["ytd_pct"] = round((p["price"] / p["base_price"] - 1) * 100, 1)
        last_day = s.index[-1].date().isoformat()
    return last_day or dt.date.today().isoformat()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recompute", action="store_true", help="räkna om listan från grunden")
    ap.add_argument("--ranking-year", type=int, help="år att ranka (standard: förra kalenderåret)")
    args = ap.parse_args()

    today = dt.date.today()
    ranking_year = args.ranking_year or today.year - 1
    buy_year = ranking_year + 1

    existing = None
    if OUT.exists():
        try:
            existing = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = None

    keep = (
        existing is not None
        and not args.recompute
        and existing.get("buy_year") == buy_year
        and len(existing.get("picks", [])) == TOP_N
    )

    if keep:
        print(f"Behåller listan för köpåret {buy_year}, uppdaterar bara kurserna")
        picks = existing["picks"]
        universe_size = existing.get("universe_size")
        ranked = existing.get("ranked_count")
        price_date = refresh_prices(picks)
    else:
        print(f"Rankar S&P 500 efter totalavkastning {ranking_year} (köpår {buy_year})")
        picks, universe_size, ranked = rank_year(ranking_year)
        price_date = today.isoformat()

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price_date": price_date,
        "ranking_year": ranking_year,
        "buy_year": buy_year,
        "universe_size": universe_size,
        "ranked_count": ranked,
        "picks": picks,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("\nTop 5 för", buy_year)
    for p in picks:
        print(f"{p['rank']}. {p['ticker']:<6} {p['name'][:28]:<28} {p['ret_prev_year_pct']:>7.1f}%  hittills i år {p['ytd_pct']:>6.1f}%")


if __name__ == "__main__":
    main()
