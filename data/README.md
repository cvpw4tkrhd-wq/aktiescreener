# data/

## watchlist.yml
Aktier som screenas. Redigera fritt — lägg till/ta bort tickers under `se:` eller `us:`.
Svenska tickers behöver Yahoo Finance-formatet med `.ST` (t.ex. `VOLV-B.ST`).

## holdings.csv
Lägg din CSV-export från Avanza eller Nordnet här, döpt till exakt `holdings.csv`.

- **Avanza**: Mina sidor → Transaktioner/Innehav → exportera som CSV. Filen
  innehåller normalt kolumnerna `Namn`, `Volym` m.fl.
- **Nordnet**: Depå → Innehav → exportera. Kolumner heter typiskt
  `Verdipapir`/`Värdipapir`, `Antal`.

Skriptet läser in filen automatiskt och matchar innehaven mot din watchlist
(på ticker om möjligt, annars på namn). Innehav som matchas får en extra
säljsignal-poäng i resultatet. Innehav som INTE finns i watchlist.yml matchas
inte — lägg till dem där också om du vill ha säljsignaler på dem.

Filen är gitignorad som standard (se `.gitignore`) så att du inte råkar
committa dina innehav till ett publikt repo av misstag.
