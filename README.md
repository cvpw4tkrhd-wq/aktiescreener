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

## Utöka

- Fler nyckeltal: lägg till i `analyze_ticker()` (yfinance `get_info()` ger
  bl.a. `priceToBook`, `dividendYield`, `marketCap`, `debtToEquity` m.fl.)
- Sektorjämförelser: gruppera P/E mot sektorsnitt istället för fasta
  trösklar.
- Notiser: lägg till ett steg i workflown som postar till Discord/Slack/mejl
  när något får hög köp- eller säljpoäng.
