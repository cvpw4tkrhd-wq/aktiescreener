#!/usr/bin/env python3
"""
Bearbetar en "Lägg till ticker"-issue (skapad automatiskt från dashboardens
"Mina innehav"-flik när en uppladdad aktie inte matchar bevakningslistan).

Validerar tickern mot Yahoo Finance, gissar sektor/land, lägger till i
data/watchlist.yml, och kommenterar/stänger issuen. Körs av
.github/workflows/add-ticker-request.yml när en issue med label "add-ticker"
skapas.
"""

import json
import os
import re
import sys
import urllib.request

import yaml
import yfinance as yf

ISSUE_BODY = os.environ.get("ISSUE_BODY", "")
ISSUE_NUMBER = os.environ.get("ISSUE_NUMBER")
REPO = os.environ.get("GITHUB_REPOSITORY")
TOKEN = os.environ.get("GITHUB_TOKEN")
WATCHLIST_PATH = "data/watchlist.yml"

SUFFIX_COUNTRY = {
    ".ST": ("SE", "Sverige"),
    ".DE": ("DE", "Tyskland"),
    ".L": ("GB", "Storbritannien"),
    ".AS": ("NL", "Nederländerna"),
    ".OL": ("NO", "Norge"),
    ".CO": ("DK", "Danmark"),
    ".HE": ("FI", "Finland"),
}

YF_SECTOR_MAP = {
    "Technology": "Technology",
    "Financial Services": "Financials",
    "Consumer Cyclical": "Consumer",
    "Consumer Defensive": "Consumer",
    "Industrials": "Industrials",
    "Healthcare": "Healthcare",
    "Energy": "Energy",
    "Basic Materials": "Materials",
    "Real Estate": "RealEstate",
    "Communication Services": "Telecom",
    "Utilities": "Energy",
}


def parse_field(label, text):
    m = re.search(rf"{label}:\s*(.+)", text)
    if not m:
        return None
    val = m.group(1).strip()
    return None if val.startswith("(") else val


def api_request(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
    )
    return urllib.request.urlopen(req)


def comment(message):
    api_request(f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}/comments", "POST", {"body": message})


def close_issue():
    api_request(f"https://api.github.com/repos/{REPO}/issues/{ISSUE_NUMBER}", "PATCH", {"state": "closed"})


def fail(message):
    comment(message)
    print(message, file=sys.stderr)
    sys.exit(0)  # inte ett workflow-fel, bara "inget att göra"


def main():
    ticker = parse_field(r"Ticker \(gissad\)", ISSUE_BODY)
    name = parse_field("Bolagsnamn", ISSUE_BODY)

    if not ticker:
        fail(
            "Kunde inte tolka en ticker från förfrågan (ingen giltig gissning fanns). "
            "Svara på den här issuen med rätt Yahoo Finance-ticker (t.ex. `VOLV-B.ST`, "
            "`AAPL`, `SAP.DE`) så kan Claude lägga till den manuellt i chatten istället."
        )

    ticker = ticker.strip().upper()

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="5d")
        info = tk.get_info()
        valid = not hist.empty and bool(info.get("longName") or info.get("shortName"))
    except Exception:
        valid = False
        info = {}

    if not valid:
        fail(
            f"Kunde inte hitta giltig data för tickern `{ticker}` på Yahoo Finance. "
            f"Kontrollera formatet (svenska aktier behöver `.ST`, tyska `.DE`, brittiska "
            f"`.L`, nederländska `.AS`, amerikanska inget suffix) och skapa en ny issue, "
            f"eller be Claude om hjälp i chatten."
        )

    market, country = "US", "USA"
    for suf, (m, c) in SUFFIX_COUNTRY.items():
        if ticker.endswith(suf):
            market, country = m, c
            break

    yf_sector = info.get("sector")
    sector = YF_SECTOR_MAP.get(yf_sector, "Consumer")
    if "semiconductor" in (info.get("industry") or "").lower():
        sector = "Semiconductors"

    display_name = name or info.get("longName") or info.get("shortName") or ticker

    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    data = yaml.safe_load(content)
    existing = {e["ticker"] for e in data["stocks"]}

    if ticker in existing:
        comment(f"`{ticker}` finns redan i bevakningslistan sedan tidigare.")
        close_issue()
        return

    data["stocks"].append({
        "ticker": ticker, "name": display_name, "sector": sector,
        "country": country, "market": market,
    })

    header = content.split("stocks:")[0] + "stocks:\n"
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(data["stocks"], f, allow_unicode=True, sort_keys=False)

    comment(
        f"Klart! `{ticker}` ({display_name}) har lagts till i bevakningslistan "
        f"(gissad sektor: {sector}, land: {country} — justera gärna sektorn manuellt "
        f"om den blev fel). Den får fullständig analys efter nästa körning "
        f"(vardagar 18:15 svensk tid, eller kör manuellt via Actions-fliken)."
    )
    close_issue()


if __name__ == "__main__":
    main()
