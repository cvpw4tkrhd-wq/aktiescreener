# Aktiescreener

En regelbaserad, transparent screener för svenska, nordiska, europeiska och
amerikanska aktier. Ger en poängsatt lista över köpkandidater (0–100,
uppdelad på köp/tillväxt/säljsignal), motiverad rad för rad, byggd som ett
första filter för egen research — **inte** finansiell rådgivning.

Körs schemalagt via GitHub Actions, dashboarden är en enda självständig
`docs/index.html`-fil som körs via GitHub Pages (samma mönster som LUFTRUM
och Poängappar).

**Live:** https://cvpw4tkrhd-wq.github.io/aktiescreener/

## Vad appen gör

- **Fem flikar:** USA, Europa, Norden och Tillväxt visar köpkandidater;
  Mina innehav visar dina egna aktier med säljsignaler.
- **Två fristående poängmodeller** — en standardmodell (balanserad mellan
  värdering, trend, kvalitet, risk) och en tillväxtmodell (samma grund, men
  utan mikro-cap-/volatilitetsstraff, för manuellt utvalda unga bolag i
  Tillväxt-fliken). Bonuspoäng dämpas efter 25 poäng (avtagande avkastning)
  så att 100 poäng förblir strukturellt sällsynt.
- **~25 nyckeltal**, bland annat P/E, PEG, forward P/E-trend, RSI, SMA20/
  50/200 (golden/death cross + en tidigare varningssignal), volym, P/B,
  utdelning, riskpremie mot riskfri ränta, intäktstillväxt, marginaltrend,
  skuldsättning, likviditet, volatilitet, FCF-marginal, aktiebaserad
  ersättning, capex/avskrivningar, **ROIC** (flerårigt snitt, minst 3 år,
  "ej tillämpligt" för banker/försäkring/fastighetsbolag), analytiker-
  konsensus (kan nedgradera en KÖP-flagga om den motsäger poängen), samt
  externa signaler: Investtech Topp 20 och ett makropanel-överlägg
  (amerikansk räntekurva + high-yield-kreditspread från FRED, gratis).
- **Egen inbyggd lärguide** (29 lektioner, bakom GUIDE-knappen) som
  förklarar varje nyckeltal med liknelser och exakta trösklar från koden,
  med progress sparad i webbläsaren.
- **Top 5-modul** (egen flik, `docs/top5.js`): en fristående S&P
  500-momentumstrategi (föregående års fem bästa aktier, köps månadsvis),
  med en rullande sparpott per aktie så att aktier som kostar mer än
  månadsdelen (t.ex. Micron) sparas ihop till en hel post istället för att
  kräva bråkdelshandel.
- **Fyra teman** (Mörkt/Ljust/Terminal/Skymning) via en knapp i headern.

## Integritet för dina innehav — viktigt att förstå

Din `holdings.csv` laddas upp **i webbläsaren** och bearbetas, matchas och
poängsätts (säljsignalen) **helt klientsidan i JavaScript**. Filen skickas
aldrig till någon server, committas aldrig till repot, och `docs/results.json`
(den fil som genereras av GitHub Actions och som vem som helst med länken
kan se) innehåller **inga** uppgifter om vilka aktier du äger eller hur
mycket.

Det betyder: du kan köra det här på ett helt publikt repo (vilket krävs för
gratis GitHub Pages) utan att dina innehav någonsin blir synliga för någon
annan, även om `docs/index.html` och `docs/results.json` är offentliga.
`data/holdings.csv` finns i `.gitignore` som ett extra skyddslager, men
skyddet ligger i arkitekturen (klientsidan), inte i att filen råkar vara
gitignorad.

## Snabbstart

1. Skapa ett nytt GitHub-repo (publikt, för gratis Pages) och pusha in den
   här mappen.
2. Aktivera GitHub Pages: Settings → Pages → Deploy from branch → `main` /
   `docs`.
3. Redigera `data/watchlist.yml` med de aktier du vill bevaka (se
   `data/README.md` för formatet).
4. Kör workflowen manuellt första gången: Actions-fliken → "Aktiescreener"
   → "Run workflow". Den körs sedan automatiskt vardagar kl 18:15 svensk
   tid (se `.github/workflows/screener.yml` för att ändra schemat).
5. Besök din Pages-URL. Ladda upp din innehavs-CSV direkt i appen (Mina
   innehav-fliken) för att se säljsignaler — det sker lokalt i din
   webbläsare, inget att konfigurera i repot.

Top 5-fliken har en egen, separat schemalagd workflow
(`.github/workflows/top5.yml`, måndagar + 1–2 januari) som uppdaterar
`docs/top5.json`. Den behöver inte konfigureras särskilt — den körs
automatiskt så fort den är på plats.

## Köra lokalt

```bash
pip install -r requirements.txt
python scripts/screener.py
# öppna docs/index.html i webbläsaren, eller:
python -m http.server -d docs 8000
```

## Hur poängen räknas

Se `scripts/screener.py` → `score_buy_candidate()` (standardmodell) och
`score_growth_candidate()` (Tillväxt-fliken). Säljpoängen i Mina
innehav-fliken beräknas i `docs/index.html` → `scoreSellSignal()`, helt
klientsidan (se integritetsavsnittet ovan för varför).

Bas 50 poäng, bonusar dämpas efter 25 poäng (avtagande avkastning), avdrag
räknas alltid fullt ut. Detaljerad, liknelsebaserad genomgång av varje
faktor finns i appens egen inbyggda guide (GUIDE-knappen).

**Inte backtestat.** Poäng- och prishistorik sparas löpande i
`data/score_history.json` sedan september 2026 för att på sikt kunna
utvärdera om poängen faktiskt korrelerat med avkastning — men det finns
ännu inget sådant facit.

## Externa datakällor (alla gratis)

- **yfinance** — kurser, nyckeltal, kassaflöde, resultat- och
  balansräkning för alla bevakade aktier
- **Financial Modeling Prep** (valfri, kräver secret `FMP_API_KEY`) —
  kompletterande kassaflödesdata för amerikanska aktier, faller tillbaka
  till yfinance om den saknas
- **FRED** (Federal Reserve) — amerikansk räntekurva och
  high-yield-kreditspread till makropanelen, ingen API-nyckel krävs
- **Investtech** — Topp 20 teknisk analys för Stockholmsbörsen,
  `data/investtech_top20.json`, uppdateras manuellt vid avstämning (inte
  live-skrapad)
- **Damodaran/NYU Stern** — statisk bransch-P/E-data för
  branschjämförelser, `docs/external_pe.json`

## AI-kvalitetsanalys (valfritt tillägg)

`scripts/qualitative_writeup.py` kör en kort kvalitativ "5-min quality
screen" via Claude API för de högst poängsatta köpkandidaterna — ovanpå
nyckeltalsscreeningen, inte istället för den.

- Tar de topp 5 köpkandidaterna med poäng ≥ 65, som inte redan är innehav
  och inte fått en analys de senaste 7 dagarna
- Anropar `claude-sonnet-4-6` med webbsökning aktiverat
- Svarar på: verksamhet + konkurrensfördel, belägg för hög avkastning på
  kapital, 2–3 största skälen till att INTE äga bolaget
- Cachar resultat i `data/writeups.json`, visas i dashboardens detaljvy
  under "AI-KVALITETSANALYS" när en analys finns

**Aktivering:** lägg till en repository secret `ANTHROPIC_API_KEY`.
Workflowen kör steget automatiskt om secreten finns — saknas den hoppas
steget bara över.

## Så läggs nya aktier till

Ingen automatiserad process längre (ett tidigare försök med
GitHub Issue-baserad automatisering, `.github/workflows/add-ticker-request.yml`
och `scripts/process_ticker_request.py`, övergavs — kändes mer krångligt än
det var värt). Nya tickers läggs till genom att be Claude om det direkt i
chatten; de två filerna ovan ligger kvar i repot men används inte.

## Utöka

- Fler nyckeltal: lägg till i `analyze_ticker()` i `scripts/screener.py`
- Fler externa signaler: samma mönster som Investtech/makropanelen — en
  statisk eller periodiskt uppdaterad referensfil i `data/`, en vikt som
  fälls in i den dämpade bonuspoolen
- Notiser: lägg till ett steg i workflowen som postar till Discord/Slack/
  mejl vid hög köp- eller säljpoäng
