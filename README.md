# Aktiescreener

Screenar svenska (Stockholmsbörsen) och amerikanska aktier på P/E, SMA50/200,
RSI14 och volymavvikelser. Ger en poängsatt lista över köpkandidater samt
säljsignaler för dina egna innehav (importerade från Avanza/Nordnet).

Körs schemalagt via GitHub Actions eller manuellt, resultatet visas i
`docs/index.html` (tänkt att köras via GitHub Pages, precis som LUFTRUM och
Poängappar).

## ⚠️ Viktigt om privata innehav och publikt repo

GitHub Pages på gratisnivån kräver ett **publikt** repo. Om du lägger dina
riktiga innehav i `data/holdings.csv` och committar dem, eller om du kör
screenern så att `docs/results.json` innehåller dina innehav, blir det
**synligt för vem som helst** som hittar sidan.

Två sätt att lösa det, välj ett innan du kör på riktigt:

1. **Privat repo + GitHub Pro/Team** (stödjer privata Pages-sidor) — enklast
   om du redan har det.
2. **Publikt repo, men håll innehaven hemliga för Actions**: lägg din CSV som
   en **repository secret** (`HOLDINGS_CSV_B64`, se nedan) istället för att
   committa filen. Workflown skriver då `data/holdings.csv` temporärt under
   körningen men filen committas aldrig (den är gitignorad). **Observera**
   dock att `docs/results.json` ändå kommer innehålla vilka aktier som är
   markerade som `is_holding: true` om de matchar din watchlist — vill du
   undvika även det, kör screenern lokalt istället för i Actions, eller ta
   bort `is_holding`/`sell_*`-fälten innan filen committas.

Standardläget (utan secret) är säkert: utan `holdings.csv` körs bara
köpscreeningen, inga innehav exponeras alls.

## Snabbstart

1. Skapa ett nytt GitHub-repo och pusha in den här mappen.
2. Aktivera GitHub Pages: Settings → Pages → Deploy from branch → `main` /
   `docs`.
3. Redigera `data/watchlist.yml` med de aktier du vill bevaka.
4. (Valfritt, se varning ovan) Lägg till din innehavs-CSV som secret:
   ```bash
   base64 -w0 data/holdings.csv | gh secret set HOLDINGS_CSV_B64
   ```
   eller manuellt i Settings → Secrets and variables → Actions.
5. Kör workflown manuellt första gången: Actions-fliken → "Aktiescreener" →
   "Run workflow". Den körs sedan automatiskt vardagar kl 18:15 svensk tid
   (se `.github/workflows/screener.yml` för att ändra schemat).
6. Besök din Pages-URL för att se dashboarden.

## Köra lokalt

```bash
pip install -r requirements.txt
python scripts/screener.py
# öppna docs/index.html i webbläsaren, eller:
python -m http.server -d docs 8000
```

## Hur poängen räknas

Se `scripts/screener.py` → `score_buy_candidate()` och `score_sell_signal()`.
Enkel, transparent regelbaserad modell (0–100) baserad på P/E, RSI-nivå,
golden/death cross mellan SMA50 och SMA200, samt volymavvikelse mot
20-dagarssnitt. Inte finansiell rådgivning — tänkt som ett första filter att
själv gräva vidare i, inte en färdig rekommendation.

## AI-kvalitetsanalys (valfritt tillägg)

`scripts/qualitative_writeup.py` kör en kort kvalitativ "5-min quality
screen" via Claude API för de högst poängsatta köpkandidaterna — ovanpå
nyckeltalsscreeningen, inte istället för den. Ersätter inte en djupanalys av
en årsredovisning.

- Tar de topp 5 köpkandidaterna med poäng ≥ 65, som inte redan är innehav
  och inte fått en analys de senaste 7 dagarna
- Anropar `claude-sonnet-4-6` med webbsökning aktiverat, och en
  investerarprofil (kvalitetstillväxt, lång horisont, moat/ROIC-fokus) som
  systemprompt — redigera `INVESTOR_IDENTITY` i skriptet för att ändra
  profilen
- Svarar på: verksamhet + konkurrensfördel, belägg för hög avkastning på
  kapital, 2–3 största skälen till att INTE äga bolaget, samt om det är en
  strukturell fördel eller kräver löpande bevakning
- Cachar resultat i `data/writeups.json`, skriver in senaste analysen i
  `docs/results.json` som fältet `ai_writeup` så dashboarden slipper ett
  extra anrop
- Visas i dashboardens detaljvy under "AI-KVALITETSANALYS" när en analys
  finns

**Aktivering:** lägg till en repository secret `ANTHROPIC_API_KEY` (skaffas
via console.anthropic.com). Workflown kör steget automatiskt efter
screenern om secreten finns — saknas den hoppas steget bara över, inget går
sönder. Notera att varje körning med nya kandidater innebär API-anrop och
därmed en kostnad; `TOP_N`, `MIN_BUY_SCORE` och `WRITEUP_MAX_AGE_DAYS`
längst upp i skriptet styr hur ofta/mycket som genereras.

## Utöka

- Fler nyckeltal: lägg till i `analyze_ticker()` (yfinance `get_info()` ger
  bl.a. `priceToBook`, `dividendYield`, `marketCap`, `debtToEquity` m.fl.)
- Sektorjämförelser: gruppera P/E mot sektorsnitt istället för fasta
  trösklar.
- Notiser: lägg till ett steg i workflown som postar till Discord/Slack/mejl
  när något får hög köp- eller säljpoäng.
