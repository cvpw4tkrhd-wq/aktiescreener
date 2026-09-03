#!/usr/bin/env python3
"""
Genererar korta kvalitativa AI-analyser ("5-min quality screen") för de
högst poängsatta köpkandidaterna från screener.py, med hjälp av Claude API.

Detta ersätter INTE en djupanalys av en årsredovisning (för det, ladda upp
rapporten till Claude själv och kör de större analysprompterna manuellt).
Detta är ett automatiskt, billigt första kvalitetsfilter ovanpå de rena
nyckeltalssignalerna: moat, ROIC-rimlighet och de största riskerna, baserat
på vad Claude kan hitta via webbsökning.

Körs efter screener.py, antingen manuellt eller i GitHub Actions. Kräver
miljövariabeln ANTHROPIC_API_KEY.

Cache: skriver till data/writeups.json. En ticker skrivs inte om på nytt
förrän WRITEUP_MAX_AGE_DAYS har gått, för att hålla antalet API-anrop och
kostnaden nere.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = ROOT / "docs"
DATA_DIR = ROOT / "data"
RESULTS_FILE = DOCS_DIR / "results.json"
WRITEUPS_FILE = DATA_DIR / "writeups.json"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"

TOP_N = 5              # max antal nya/omgenererade skriv-ups per körning
MIN_BUY_SCORE = 65      # hoppa över kandidater under denna poäng
WRITEUP_MAX_AGE_DAYS = 7  # generera inte om en ticker som skrevs nyligen

INVESTOR_IDENTITY = """Du är min personliga investeringsanalytiker. Tillämpa
detta ramverk:

FILOSOFI: Långsiktig kvalitetstillväxt-investerare, 5-10 års horisont.
Fokus på hållbara konkurrensfördelar (moats) och hög avkastning på investerat
kapital (ROIC helst över 15%). Föredrar bolag som kan återinvestera stora
delar av vinsten till hög avkastning (compounders). Kapitalallokering och
ägartänkande hos ledningen är viktigt. Villig att betala rimligt pris för ett
exceptionellt bolag, men prismedveten.

VAD JAG VILL HA: Balanserad, ärlig analys - inte översäljande. Konkreta
belägg, inte vaga påståenden. Tydlig markering när något är osäkert eller
saknar stöd i informationen du hittar.

SPRÅK: Skriv för någon som INTE är insatt i finansjargong. Om du använder ett
fackuttryck (t.ex. "moat", "ROIC", "marginal", "utspädning") - förklara det
kort i samma mening, i vardagliga ord. Undvik onödigt komplicerade
formuleringar. Korta meningar.

VAD JAG INTE VILL HA: Ytliga sammanfattningar. Överdrivet positiv vinkling.
Finansiella prognoser framställda som fakta."""


def load_results():
    with open(RESULTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_writeups():
    if WRITEUPS_FILE.exists():
        with open(WRITEUPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_writeups(writeups):
    DATA_DIR.mkdir(exist_ok=True)
    with open(WRITEUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(writeups, f, ensure_ascii=False, indent=2)


def is_stale(entry):
    if not entry:
        return True
    try:
        generated_at = datetime.fromisoformat(entry["generated_at"])
    except (KeyError, ValueError):
        return True
    age_days = (datetime.now(timezone.utc) - generated_at).total_seconds() / 86400
    return age_days >= WRITEUP_MAX_AGE_DAYS


def build_prompt(d):
    signals = {
        "namn": d.get("name") or d.get("watchlist_name"),
        "ticker": d.get("ticker"),
        "sektor": d.get("sector"),
        "pris": d.get("price"),
        "valuta": d.get("currency"),
        "pe": d.get("pe"),
        "pb": d.get("pb"),
        "utdelning_pct": d.get("dividend_yield_pct"),
        "skuldsattning_de": d.get("debt_to_equity"),
        "rsi14": d.get("rsi14"),
        "trend_signal": d.get("cross_signal"),
        "analytikerrek": d.get("recommendation_key"),
        "antal_analytiker": d.get("num_analysts"),
        "analytiker_uppsida_pct": d.get("analyst_upside_pct"),
        "screener_koppoang": d.get("buy_score"),
        "screener_skal": d.get("buy_reasons"),
    }
    return f"""Screenern har flaggat följande bolag som en köpkandidat baserat
på rena nyckeltal och teknisk analys (P/E, RSI, glidande medelvärden,
analytikerkonsensus). Din uppgift är INTE att upprepa dessa siffror, utan att
lägga till det de inte fångar: en snabb kvalitetsbedömning av själva
verksamheten, skriven så att någon utan finansbakgrund förstår den.

Nyckeltal från screenern (JSON):
{json.dumps(signals, ensure_ascii=False, indent=2)}

Sök på webben vid behov för att svara på:

1. Vilken verksamhet bedriver bolaget, och vad är den påstådda
   konkurrensfördelen? (Förklara enkelt vad som gör det svårt för
   konkurrenter att ta marknadsandelar.)
2. Finns det offentligt tillgängliga belägg för att bolaget är lönsamt och
   ger god avkastning på det kapital som satsas i verksamheten, eller är det
   osäkert utifrån vad du hittar?
3. Vilka är de 2-3 största skälen till att detta INTE skulle vara ett
   kvalitetsbolag värt att äga långsiktigt?
4. Är detta ett bolag som kräver kvartalsvis bevakning (risken ligger i att
   den dagliga driften/exekveringen måste fungera) eller ett där fördelen är
   strukturell och varaktig (svår att rubba oavsett kvartal)?

FORMAT (viktigt, följ exakt):
- Skriv i korta stycken med en **fet etikett** i början av varje stycke
  (t.ex. "**Verksamhet:** ...", "**Lönsamhet:** ...", "**Risker:** ...").
  Använd INTE rubriker med #, ## eller ### och INTE avskiljare som "---".
- Använd punktlistor (- eller 1. 2. 3.) för uppräkningar, inte långa
  meningar med semikolon.
- Avsluta med EXAKT en rad i detta format (ingen extra text efter):
  HELHETSBILD: <Positiv|Blandad|Försiktig> – <en kort mening som sammanfattar
  varför, i vardagsspråk>
  Använd "Positiv" om helhetsbilden talar för bolaget som kvalitetsinvestering,
  "Försiktig" om de stora riskerna/varningstecknen väger tyngre än fördelarna,
  annars "Blandad".

Max ca 280 ord totalt (exklusive HELHETSBILD-raden). Var ärlig och
balanserad, inte översäljande. Skriv för en nybörjare - förklara alla
fackuttryck du använder."""


def call_claude(prompt: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY saknas i miljön.")

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1600,
        "system": INVESTOR_IDENTITY,
        "messages": [{"role": "user", "content": prompt}],
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    text_parts = [block["text"] for block in data.get("content", []) if block.get("type") == "text"]
    return "\n\n".join(text_parts).strip()


def main():
    if not RESULTS_FILE.exists():
        print(f"Hittar inte {RESULTS_FILE} - kör screener.py först.", file=sys.stderr)
        sys.exit(1)

    output = load_results()
    results = output.get("results", [])
    writeups = load_writeups()

    candidates = [
        d for d in results
        if not d.get("is_holding") and (d.get("buy_score") or 0) >= MIN_BUY_SCORE
    ]
    candidates.sort(key=lambda d: d.get("buy_score", 0), reverse=True)

    to_process = [d for d in candidates if is_stale(writeups.get(d["ticker"]))][:TOP_N]

    if not to_process:
        print("Inga kandidater behöver en (ny) AI-analys just nu.", file=sys.stderr)
    else:
        print(f"Genererar AI-analys för {len(to_process)} kandidat(er)...", file=sys.stderr)

    for d in to_process:
        ticker = d["ticker"]
        print(f"  {ticker}...", file=sys.stderr)
        try:
            prompt = build_prompt(d)
            text = call_claude(prompt)
            writeups[ticker] = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "buy_score_at_generation": d.get("buy_score"),
                "model": MODEL,
                "text": text,
            }
        except (urllib.error.URLError, RuntimeError, ValueError) as e:
            print(f"    Fel för {ticker}: {e}", file=sys.stderr)
        time.sleep(1)

    save_writeups(writeups)

    # Skriv in senaste writeup i results.json så dashboarden kan visa den
    # direkt utan ett extra fetch-anrop.
    for d in results:
        w = writeups.get(d["ticker"])
        if w:
            d["ai_writeup"] = {
                "text": w["text"],
                "generated_at": w["generated_at"],
            }

    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("Klart.", file=sys.stderr)


if __name__ == "__main__":
    main()
