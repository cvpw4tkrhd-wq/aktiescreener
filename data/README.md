# data/

## watchlist.yml
Bevakningslistan, en platt lista med ett objekt per aktie (inte grupperad
under `se:`/`us:` — se filens egen kommentar-header för fältbeskrivning).

```yaml
stocks:
- ticker: VOLV-B.ST
  name: Volvo B
  sector: Industrials
  country: Sverige
  market: SE
  growth_candidate: true   # valfritt, taggar den för Tillväxt-fliken
```

- **ticker**: Yahoo Finance-format. Suffix per marknad: Sverige `.ST`,
  Norge `.OL`, Danmark `.CO`, Finland `.HE`, Tyskland `.DE`, Nederländerna
  `.AS`, Storbritannien `.L`, USA inget suffix. **Brittiska aktier** anges
  ofta i pence (GBp) av Yahoo — appen konverterar automatiskt till GBP,
  inget att tänka på här.
- **name**: visningsnamn, används även som fallback vid matchning mot
  innehav om tickern inte matchar exakt.
- **sector**: används för geopolitik/makro-riskfaktorer (`risk_factors.yml`),
  ROIC:s "ej tillämpligt"-logik (Financials/RealEstate), och
  sammanfattningen på Mina innehav-fliken. Fria textvärden, men håll dem
  konsekventa (`Technology`, `Financials`, `Industrials`, `Healthcare`,
  `Energy`, `Materials`, `Consumer`, `RealEstate`, `Telecom`,
  `Semiconductors` används idag).
- **country**: används för landssammanfattningen på Mina innehav-fliken.
- **market**: kort marknadskod, styr vilken flik aktien hamnar i
  (SE/NO/DK/FI → Norden, US → USA, övriga → Europa).
- **growth_candidate** (valfritt, `true`/`false`): taggar aktien för
  Tillväxt-fliken, som använder en egen poängmodell (ingen mikro-cap-/
  volatilitetsstraff) och kräver ≥40 poäng i den modellen för att synas.
  Manuellt kuraterat, ingen automatisk urvalslogik.

Nya aktier läggs till genom att be Claude om det i chatten — ingen
automatiserad process (se root-READMEn för bakgrund).

## holdings.csv
Lägg din CSV-export från Avanza eller Nordnet här, döpt till exakt
`holdings.csv` — **men ladda upp den i appens gränssnitt (Mina
innehav-fliken), inte genom att committa filen till repot.**

- **Avanza**: Mina sidor → Transaktioner/Innehav → exportera som CSV.
- **Nordnet**: Depå → Innehav → exportera.

Filen bearbetas, matchas mot watchlist.yml (på ticker om möjligt, annars på
namn) och poängsätts **helt i din egen webbläsare**. Den skickas aldrig
till GitHub Actions, screener.py rör den aldrig, och `docs/results.json`
innehåller ingen information om dina innehav. En lokal kopia i repot
(`data/holdings.csv`, gitignorad) är bara kvar av historiska skäl/lokala
tester — den normala vägen är uppladdning i webbläsaren.

Innehav som INTE finns i watchlist.yml matchas inte — lägg till dem där
också om du vill ha säljsignaler på dem.

## Övriga filer i den här mappen

- **risk_factors.yml** — geopolitiska sektor-/landsvikter, fälls in i
  samma dämpade bonuspool som allt annat. Uppdateras bara efter att Claude
  sökt fram och källbelagt en aktuell nyhet, aldrig automatiskt.
- **risk_free_rates.json** — landsspecifika 10-åriga statsobligationsräntor,
  används för riskpremie-beräkningen. Statisk, uppdateras manuellt.
- **investtech_top20.json** — Investtechs Topp 20 (teknisk analys,
  Stockholmsbörsen). Manuellt uppdaterad vid avstämning, inte live-skrapad.
- **score_history.json** — daglig poäng- och prishistorik per ticker,
  cirka 1,5 års retention. Underlag för en framtida backtest, inget som
  används aktivt i poängmodellen ännu.
- **writeups.json** — cache för AI-kvalitetsanalyserna (se root-READMEn),
  max en ny analys per ticker var 7:e dag.
- **price_snapshots/** — enstaka sparade ögonblicksbilder för
  jämförelseändamål.
- **analysis_framework.md** — sökguide Claude använder som referens när
  `risk_factors.yml` ska stämmas av, inte en lista att mekaniskt poängsätta
  rakt av.
