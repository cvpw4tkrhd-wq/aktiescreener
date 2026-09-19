/* Top 5 – månadsköp
 *
 * Fristående modul för Screener-dashboarden. Lägger själv till fliken TOP 5 och en
 * egen panel och rör inget annat på sidan. Läser top5.json (skapas av scripts/top5.py).
 * Bockar, belopp och startmånad sparas bara lokalt i webbläsaren (localStorage).
 */
(function () {
  'use strict';

  var K_BOUGHT = 'top5_bought_v2';   // { "2026-09": { avail: ["WDC"], done: ["WDC"], shares: {"WDC":1} } }
  var K_AMOUNT = 'top5_amount_v1';   // totalt belopp per månad i kr
  var K_START = 'top5_start_v1';     // "2026-09" – första månaden du följt listan
  var K_BANKED = 'top5_banked_v1';   // { "WDC": 234.5, ... } – sparad, ännu inte köpt summa per aktie
  var K_TOPUP = 'top5_topup_v1';     // { "WDC": "2026-09" } – senaste månad kontot fylldes på (skyddar mot dubbel påfyllning)
  var DEFAULT_AMOUNT = 5000;

  var MONTHS = ['jan', 'feb', 'mar', 'apr', 'maj', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];
  var MONTHS_LONG = ['januari', 'februari', 'mars', 'april', 'maj', 'juni', 'juli', 'augusti',
    'september', 'oktober', 'november', 'december'];
  var SECTORS = {
    'Information Technology': 'Teknik', 'Health Care': 'Hälsovård', 'Financials': 'Finans',
    'Consumer Discretionary': 'Sällanköpsvaror', 'Consumer Staples': 'Dagligvaror',
    'Industrials': 'Industri', 'Energy': 'Energi', 'Utilities': 'Kraftförsörjning',
    'Materials': 'Material', 'Real Estate': 'Fastigheter', 'Communication Services': 'Kommunikation'
  };

  // Historiskt test 2006–2025: [år, värde Top 5, värde index] i tusen kr.
  // Värdet i aug 2026 av 12 × 5 000 kr som köptes under året.
  var HIST = [[2006, 2140, 510], [2007, 4633, 443], [2008, 1868, 522], [2009, 418, 679],
    [2010, 2313, 542], [2011, 628, 472], [2012, 415, 428], [2013, 279, 355], [2014, 683, 295],
    [2015, 608, 269], [2016, 2653, 260], [2017, 1102, 218], [2018, 111, 190], [2019, 371, 179],
    [2020, 387, 159], [2021, 187, 118], [2022, 79, 118], [2023, 99, 113], [2024, 83, 89],
    [2025, 73, 76]];

  // ---------- hjälpfunktioner ----------
  function lsGet(k, d) {
    try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; }
  }
  function lsSet(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* privat läge m.m. */ }
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmtInt(n) { return Math.round(n).toLocaleString('sv-SE'); }
  function ym(d) { return d.getFullYear() + '-' + pad(d.getMonth() + 1); }
  function fmtDate(d) { return d.getDate() + ' ' + MONTHS_LONG[d.getMonth()]; }
  function pct(n) {
    if (n === null || n === undefined || isNaN(n)) return '–';
    var cls = n >= 0 ? 't5-pos' : 't5-neg';
    var s = (n >= 0 ? '+' : '\u2212') + fmtInt(Math.abs(n)) + ' %';
    return '<span class="' + cls + '">' + s + '</span>';
  }
  function dayDiff(a, b) { return Math.round((b - a) / 86400000); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  // Första handelsdagen i månaden (första vardagen, exkl. nyårsdagen och Labor Day).
  function firstTradingDay(year, m0) {
    var d = new Date(year, m0, 1);
    for (var i = 0; i < 10; i++) {
      var dow = d.getDay();
      var weekend = dow === 0 || dow === 6;
      var newYear = d.getMonth() === 0 && d.getDate() === 1;
      var labor = d.getMonth() === 8 && dow === 1 && d.getDate() <= 7;
      if (!weekend && !newYear && !labor) return d;
      d.setDate(d.getDate() + 1);
    }
    return d;
  }

  // ---------- statisk text ----------
  var STATIC_HTML =
    '<div class="t5-sec">' +
    '<details open><summary>Så fungerar strategin</summary>' +
    '<div class="t5-body">' +
    '<p>Idén är momentum: aktier som klarat sig allra bäst det senaste året har historiskt ofta fortsatt slå marknaden en tid till, men inte alltid. Strategin satsar därför inte på en enda aktie utan sprider sig över fem.</p>' +
    '<ol class="t5-steps">' +
    '<li><b>Vid årsskiftet</b> rankas alla bolag i S&amp;P 500 efter totalavkastning (kursutveckling plus utdelningar) under det gångna kalenderåret.</li>' +
    '<li><b>De fem bästa</b> blir årets lista. Den visas överst här och gäller hela året.</li>' +
    '<li><b>Första handelsdagen varje månad</b> köper du för lika mycket i var och en av de fem. Med 5 000 kr i månaden blir det 1 000 kr per aktie – men eftersom aktier bara går att köpa i hela poster sparas beloppet automatiskt ihop tills det räcker till en hel aktie, om en enskild aktie kostar mer än din månadsdel (se "Hela aktier, inte procent" nedan).</li>' +

    '<li><b>Sälj inte.</b> Inga stop-loss och ingen ombalansering. I våra tester gav stop-loss sämre resultat, eftersom vinnare ofta svänger kraftigt innan de stiger.</li>' +
    '<li><b>Nytt år, ny lista.</b> Från januari köper du de nya fem. Förra årets aktier ligger kvar orörda.</li>' +
    '</ol>' +
    '<h4>Varför fem aktier i stället för en?</h4>' +
    '<p>En enda aktie kan falla hårt. Natl Oilwell, som var årets vinnare inför 2008, gav bara 0,58 gånger pengarna när du köpte den månadsvis. Med fem aktier var det sämsta årsutfallet i testet 1,22 gånger pengarna, och inget år gick med förlust.</p>' +
    '<h4>Varför månadsköp?</h4>' +
    '<p>Inköpen sprids över tolv månader, så ett dåligt inköpstillfälle väger mindre. Det är också lättare att hålla i praktiken än att hitta ett enda rätt ögonblick.</p>' +
    '<h4>Hela aktier, inte procent</h4>' +
    '<p>Mäklare säljer normalt inte delar av en aktie – du kan inte köpa "0,1 Micron-aktier" för 1 000 kr om aktien kostar 10 000 kr. Därför sparas din månadsdel för en sådan aktie automatiskt ihop i en egen liten pott tills den räcker till en hel post. Du ser hur mycket som är sparat för varje aktie i listan nedan, och behöver bara klicka i en bock den månaden potten faktiskt räcker till ett köp. Det gör att fördelningen mellan de fem förblir ungefär jämn över tid, bara utspridd i tid istället för varje månad.</p>' +
    '<h4>Att tänka på</h4>' +
    '<ul class="t5-list">' +
    '<li>Momentumlistor är ofta koncentrerade till samma bransch. Flera aktier från samma sektor betyder mer risk än fem aktier låter som.</li>' +
    '<li>Aktier som stigit 200–400 % på ett år kan falla lika snabbt. Räkna med perioder då strategin ligger efter indexet. Det hände 8 av 20 år i testet.</li>' +
    '<li>Kurserna är i dollar. Kolla courtage och valutaavgift hos din bank eller mäklare: 1 000 kr per aktie och månad kan ge hög procentuell avgift. Ett alternativ är att köpa var tredje månad för ett större belopp.</li>' +
    '<li>Det här är ett historiskt test, inte en prognos och inte finansiell rådgivning.</li>' +
    '</ul>' +
    '</div></details></div>' +

    '<div class="t5-sec">' +
    '<details><summary>Så gick det historiskt (2006–2025)</summary>' +
    '<div class="t5-body">' +
    '<p>Testet: 5 000 kr per månad, fördelat lika på årets Top 5, orört till augusti 2026. Jämfört med samma insättningar i S&amp;P 500.</p>' +
    '<ul class="t5-list">' +
    '<li>1,2 milj kr insatta blev cirka <b>19,1 milj kr</b> med Top 5, mot cirka <b>6,0 milj kr</b> i indexet.</li>' +
    '<li>Top 5 slog indexet 12 av 20 år. Sämsta årsomgången gav 1,22 gånger pengarna.</li>' +
    '<li>Hälften av resultatet kom från tre år (2007, 2010 och 2016). Utan dem blir det 9,5 mot 4,8 milj kr. Det är fortfarande före indexet, men mycket mindre.</li>' +
    '<li>De tio bästa i stället för fem gav bara 13,2 milj kr. Slutsumman beror alltså mycket på vilka få jättevinnare som kom med, till exempel Nvidia, Apple, Micron och AMD.</li>' +
    '</ul>' +
    '<div class="t5-hist" role="table" aria-label="Historiskt utfall per år">' +
    '<div class="t5-hh" role="row"><span>Köpår</span><span>Top 5</span><span>Index</span></div>' +
    HIST.map(function (r) {
      var win = r[1] >= r[2];
      return '<div class="t5-hr" role="row"><span>' + r[0] + '</span><span class="' + (win ? 't5-pos' : 't5-neg') + '">' +
        fmtInt(r[1]) + '</span><span>' + fmtInt(r[2]) + '</span></div>';
    }).join('') +
    '</div>' +
    '<p class="t5-small">Tabellen visar värdet i augusti 2026 av årets 12 köp à 5 000 kr, i tusen kronor. Äldre år har haft längre tid att växa.</p>' +
    '<h4>Begränsningar</h4>' +
    '<p>Kursdata saknas för många nedlagda och uppköpta bolag (ungefär en tredjedel i de tidiga åren), vilket sannolikt gör resultatet för gott. Beräkningen är i dollar, utan courtage, skatt och valutaeffekter, och tjugo år är få observationer.</p>' +
    '</div></details></div>' +

    '<div class="t5-sec t5-foot">Bockar och belopp sparas bara i den här webbläsaren. Inte finansiell rådgivning.</div>';

  var CSS =
    '#tabsRow{overflow-x:auto;overflow-y:hidden;padding-bottom:1px;margin-bottom:-1px;scrollbar-width:none;-webkit-overflow-scrolling:touch}' +
    '#tabsRow::-webkit-scrollbar{display:none}' +
    '#tabsRow .tab{flex:0 0 auto;white-space:nowrap}' +
    '.t5 h2,.t5 h3,.t5 h4,.t5 p,.t5 ul,.t5 ol{margin:0}' +
    '.t5-sec{padding:16px 18px;border-bottom:1px solid var(--border)}' +
    '.t5-sec:last-child{border-bottom:none}' +
    '.t5-title{font-family:"Fraunces",serif;font-weight:600;font-size:21px;letter-spacing:-0.01em}' +
    '.t5-lede{color:var(--text-dim);font-size:13.5px;line-height:1.55;margin-top:4px!important}' +
    '.t5-status{margin-top:14px;padding:12px 14px;border:1px solid var(--border);border-left-width:3px;border-radius:var(--radius);background:var(--panel-alt)}' +
    '.t5-status.due,.t5-status.late{border-left-color:var(--amber)}' +
    '.t5-status.ok{border-left-color:var(--up)}' +
    '.t5-status.wait{border-left-color:var(--text-faint)}' +
    '.t5-st-t{font-size:15px;font-weight:600}' +
    '.t5-st-s{font-size:12.5px;color:var(--text-dim);margin-top:3px!important;line-height:1.45}' +
    '.t5-amount{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:14px;font-size:13px;color:var(--text-dim)}' +
    '.t5-amount input{width:96px;font-family:"IBM Plex Mono",monospace;font-size:14px;padding:7px 9px;background:var(--panel-alt);color:var(--text);border:1px solid var(--border);border-radius:var(--radius)}' +
    '.t5-rows{margin-top:10px}' +
    '.t5-row{display:flex;gap:12px;align-items:flex-start;padding:12px 0;border-top:1px solid var(--border);cursor:pointer;min-height:44px;-webkit-tap-highlight-color:transparent}' +
    '.t5-row input{width:22px;height:22px;margin:2px 0 0;flex:0 0 auto;accent-color:var(--up)}' +
    '.t5-main{flex:1 1 auto;min-width:0}' +
    '.t5-line{display:flex;gap:8px;align-items:baseline;min-width:0}' +
    '.t5-tk{font-family:"IBM Plex Mono",monospace;font-weight:600;font-size:15px}' +
    '.t5-nm{color:var(--text-dim);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.t5-meta{display:flex;flex-wrap:wrap;gap:2px 12px;margin-top:4px;font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--text-faint)}' +
    '.t5-amt{font-family:"IBM Plex Mono",monospace;font-size:13px;white-space:nowrap;padding-top:2px}' +
    '.t5-row.done .t5-tk,.t5-row.done .t5-nm{opacity:.5}' +
    '.t5-row-saving{opacity:.7}' +
    '.t5-saving-amt{color:var(--text-faint)}' +
    '.t5-pos{color:var(--up)}.t5-neg{color:var(--down)}' +
    '.t5-note{margin-top:10px!important;padding:10px 12px;border:1px solid var(--amber-dim);border-radius:var(--radius);font-size:12.5px;line-height:1.5;color:var(--text-dim)}' +
    '.t5-warn{margin-top:14px!important;padding:10px 12px;border:1px solid var(--down);border-radius:var(--radius);font-size:12.5px;line-height:1.5}' +
    '.t5-months{display:grid;grid-template-columns:repeat(12,1fr);gap:3px;margin-top:14px}' +
    '.t5-mo{font-family:"IBM Plex Mono",monospace;font-size:10px;text-align:center;padding:6px 0;border:1px solid var(--border);border-radius:var(--radius);color:var(--text-faint)}' +
    '.t5-mo.done{background:var(--up);border-color:var(--up);color:var(--bg)}' +
    '.t5-mo.part{border-color:var(--amber);color:var(--amber)}' +
    '.t5-mo.miss{border-color:var(--down);color:var(--down)}' +
    '.t5-mo.cur{border-color:var(--text);color:var(--text)}' +
    '.t5-mo.off{border-style:dashed;opacity:.5}' +
    '.t5-legend{font-size:11px;color:var(--text-faint);margin-top:6px!important}' +
    '.t5-actions{margin-top:14px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}' +
    '.t5 summary{cursor:pointer;font-weight:600;font-size:15px;padding:2px 0}' +
    '.t5-body{margin-top:10px;font-size:13.5px;line-height:1.6;color:var(--text)}' +
    '.t5-body p{margin-top:8px}' +
    '.t5-body h4{font-size:13.5px;margin-top:16px;font-weight:600}' +
    '.t5-steps,.t5-list{padding-left:20px;margin-top:8px}' +
    '.t5-steps li,.t5-list li{margin-top:7px}' +
    '.t5-hist{margin-top:12px;font-family:"IBM Plex Mono",monospace;font-size:12.5px}' +
    '.t5-hh,.t5-hr{display:grid;grid-template-columns:64px 1fr 1fr;gap:8px;padding:5px 0;border-top:1px solid var(--border)}' +
    '.t5-hh{color:var(--text-faint);font-size:11.5px;border-top:none}' +
    '.t5-hh span:not(:first-child),.t5-hr span:not(:first-child){text-align:right}' +
    '.t5-small{font-size:11.5px!important;color:var(--text-faint)}' +
    '.t5-foot{font-size:11.5px;color:var(--text-faint)}';

  // ---------- modulens tillstånd ----------
  var data = null;
  var loadError = null;
  var active = false;
  var tab, panel, dataPanel, summaryHost;

  function injectCss() {
    var st = document.createElement('style');
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  // ---------- månadskortet ----------
  function renderMonth() {
    var host = document.getElementById('t5Month');
    if (!host) return;

    if (loadError) {
      host.innerHTML = '<div class="t5-sec"><div class="t5-title">Top 5</div>' +
        '<p class="t5-warn">Kunde inte läsa top5.json. Kör workflowet &quot;Top 5&quot; under Actions i GitHub så skapas filen. (' + esc(loadError) + ')</p></div>';
      return;
    }
    if (!data) {
      host.innerHTML = '<div class="t5-sec"><div class="t5-title">Top 5</div><p class="t5-lede">Laddar listan …</p></div>';
      return;
    }

    var picks = data.picks || [];
    var now = new Date();
    var today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var key = ym(today);
    var ftd = firstTradingDay(today.getFullYear(), today.getMonth());
    var start = lsGet(K_START, null);
    if (!start) { start = key; lsSet(K_START, start); }

    var total = Number(lsGet(K_AMOUNT, DEFAULT_AMOUNT));
    if (!isFinite(total) || total < 0) total = DEFAULT_AMOUNT;
    var per = picks.length ? total / picks.length : 0;

    // ---- Rullande sparpott per aktie ----
    // Varje akties månadsdel läggs till en egen pott en gång per månad (skyddat
    // mot dubbel påfyllning vid omladdning via K_TOPUP). Räcker potten inte till
    // en hel post krävs ingen åtgärd - den fylls på automatiskt nästa månad.
    var banked = lsGet(K_BANKED, {});
    var topup = lsGet(K_TOPUP, {});
    var bought = lsGet(K_BOUGHT, {});
    var monthRec = bought[key] || { avail: [], done: [], shares: {} };
    if (!monthRec.shares) monthRec.shares = {};

    var toppedUpNow = false;
    picks.forEach(function (p) {
      if (topup[p.ticker] !== key) {
        banked[p.ticker] = (banked[p.ticker] || 0) + per;
        topup[p.ticker] = key;
        toppedUpNow = true;
      }
    });
    if (toppedUpNow) { lsSet(K_BANKED, banked); lsSet(K_TOPUP, topup); }

    var rowsData = picks.map(function (p) {
      var pot = banked[p.ticker] || 0;
      var qty = p.price > 0 ? Math.floor(pot / p.price) : 0;
      var cost = qty * p.price;
      return { p: p, pot: pot, qty: qty, cost: cost, available: qty >= 1, done: monthRec.done.indexOf(p.ticker) > -1 };
    });

    var availTickers = rowsData.filter(function (r) { return r.available; }).map(function (r) { return r.p.ticker; });
    monthRec.avail = availTickers;
    bought[key] = monthRec;
    lsSet(K_BOUGHT, bought);

    var allDone = availTickers.length > 0 && availTickers.every(function (tk) { return monthRec.done.indexOf(tk) > -1; });
    var nothingToDo = availTickers.length === 0;
    var nextFtd = firstTradingDay(today.getMonth() === 11 ? today.getFullYear() + 1 : today.getFullYear(),
      (today.getMonth() + 1) % 12);

    var st;
    if (nothingToDo) {
      st = { cls: 'wait', t: 'Inget att köpa den här månaden',
        s: 'Ingen aktie har sparat ihop till en hel post ännu. Beloppet läggs på automatiskt igen ' + MONTHS_LONG[nextFtd.getMonth()] + '.' };
    } else if (allDone) {
      st = { cls: 'ok', t: 'Klart för ' + MONTHS_LONG[today.getMonth()],
        s: 'Nästa avstämning: ' + fmtDate(nextFtd) + ' (om ' + plural(dayDiff(today, nextFtd), 'dag', 'dagar') + ').' };
    } else if (today < ftd) {
      st = { cls: 'wait', t: 'Nästa köptillfälle: ' + fmtDate(ftd),
        s: 'Om ' + plural(dayDiff(today, ftd), 'dag', 'dagar') + ' är det första handelsdagen i ' + MONTHS_LONG[today.getMonth()] + '.' };
    } else if (dayDiff(ftd, today) === 0) {
      st = { cls: 'due', t: 'Idag är första handelsdagen',
        s: 'Köp de aktier nedan som räckt till en hel post, och bocka av dem.' };
    } else {
      st = { cls: 'late', t: 'Något återstår att köpa',
        s: 'Första handelsdagen var ' + fmtDate(ftd) + '. Köp och bocka av när det är gjort.' };
    }

    // Sektorkoncentration
    var counts = {};
    picks.forEach(function (p) { counts[p.sector] = (counts[p.sector] || 0) + 1; });
    var topSector = null, topN = 0;
    Object.keys(counts).forEach(function (s) { if (s && counts[s] > topN) { topN = counts[s]; topSector = s; } });

    var rows = rowsData.map(function (r) {
      var p = r.p;
      var meta = '<div class="t5-meta"><span>' + esc(data.ranking_year) + ': ' + pct(p.ret_prev_year_pct) + '</span>' +
        '<span>hittills i år: ' + pct(p.ytd_pct) + '</span>' +
        '<span>' + esc(SECTORS[p.sector] || p.sector || '') + '</span></div>';

      if (!r.available) {
        return '<div class="t5-row t5-row-saving">' +
          '<div class="t5-main"><div class="t5-line"><span class="t5-tk">' + esc(p.ticker) + '</span>' +
          '<span class="t5-nm">' + esc(p.name) + '</span></div>' + meta + '</div>' +
          '<div class="t5-amt t5-saving-amt" data-amt>Sparar ' + fmtInt(r.pot) + ' / ' + fmtInt(p.price) + ' kr</div></div>';
      }
      return '<label class="t5-row' + (r.done ? ' done' : '') + '">' +
        '<input type="checkbox" data-tk="' + esc(p.ticker) + '"' + (r.done ? ' checked' : '') + '>' +
        '<div class="t5-main"><div class="t5-line"><span class="t5-tk">' + esc(p.ticker) + '</span>' +
        '<span class="t5-nm">' + esc(p.name) + '</span></div>' + meta + '</div>' +
        '<div class="t5-amt" data-amt>Köp ' + plural(r.qty, 'st', 'st') + ' (' + fmtInt(r.cost) + ' kr)</div></label>';
    }).join('');

    var thisYear = today.getFullYear();
    var chips = '';
    for (var m = 0; m < 12; m++) {
      var mk = thisYear + '-' + pad(m + 1);
      var rec = bought[mk];
      var cls;
      if (mk < start) cls = 'off';
      else if (mk > key) cls = '';
      else if (mk === key) cls = (nothingToDo || allDone) ? 'done' : 'cur';
      else if (!rec || !rec.avail) cls = 'done'; // äldre data (innan sparpotten fanns) eller inget att göra den månaden
      else {
        var doneCount = rec.done ? rec.done.length : 0;
        cls = rec.avail.length === 0 ? 'done' : (doneCount >= rec.avail.length ? 'done' : (doneCount > 0 ? 'part' : 'miss'));
      }
      chips += '<div class="t5-mo ' + cls + '">' + MONTHS[m] + '</div>';
    }

    var stale = data.buy_year !== thisYear
      ? '<p class="t5-warn">Listan gäller köpåret ' + esc(data.buy_year) + ', men det är nu ' + thisYear +
        '. Kör workflowet &quot;Top 5&quot; under Actions i GitHub och bocka i &quot;Räkna om&quot; för att få årets lista.</p>'
      : '';

    var conc = (topN >= 3)
      ? '<p class="t5-note">' + topN + ' av ' + picks.length + ' aktier är från sektorn ' +
        esc(SECTORS[topSector] || topSector) + '. Risken är därför mer koncentrerad än fem aktier låter som.</p>'
      : '';

    host.innerHTML =
      '<div class="t5-sec">' +
      '<h2 class="t5-title">Månadens köp</h2>' +
      '<p class="t5-lede">Årets fem bästa S&amp;P 500-aktier från ' + esc(data.ranking_year) +
      '. Varje aktie får en lika stor andel av månadsbeloppet, sparad tills den räcker till en hel post. Sälj inte.</p>' +
      stale +
      '<div class="t5-status ' + st.cls + '"><p class="t5-st-t">' + esc(st.t) + '</p><p class="t5-st-s">' + esc(st.s) + '</p></div>' +
      '<div class="t5-amount"><label for="t5Amount">Belopp per månad</label>' +
      '<input id="t5Amount" type="number" inputmode="numeric" min="0" step="500" value="' + Math.round(total) + '"><span>kr</span>' +
      '<span id="t5Per">= ' + fmtInt(per) + ' kr per aktie och månad</span></div>' +
      '<div class="t5-rows">' + rows + '</div>' +
      conc +
      '<div class="t5-months" aria-label="Månader ' + thisYear + '">' + chips + '</div>' +
      '<p class="t5-legend">Fylld = klart eller inget att göra, gul kant = delvis, röd kant = missat köp. Kurser per ' + esc(data.price_date) + ' i USD.</p>' +
      '<div class="t5-actions"><button class="iconbtn" data-act="ics" type="button">Lägg månadspåminnelse i kalendern</button></div>' +
      '</div>';
  }

  // ---------- kalenderpåminnelse (.ics) ----------
  function downloadIcs() {
    var now = new Date();
    var ftd = firstTradingDay(now.getFullYear(), now.getMonth());
    var first = ftd >= new Date(now.getFullYear(), now.getMonth(), now.getDate())
      ? ftd
      : firstTradingDay(now.getMonth() === 11 ? now.getFullYear() + 1 : now.getFullYear(), (now.getMonth() + 1) % 12);
    var d8 = first.getFullYear() + pad(first.getMonth() + 1) + pad(first.getDate());
    var stamp = now.getUTCFullYear() + pad(now.getUTCMonth() + 1) + pad(now.getUTCDate()) + 'T' +
      pad(now.getUTCHours()) + pad(now.getUTCMinutes()) + pad(now.getUTCSeconds()) + 'Z';
    var lines = [
      'BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Screener//Top 5//SV', 'CALSCALE:GREGORIAN',
      'BEGIN:VEVENT', 'UID:top5-manadskop@screener', 'DTSTAMP:' + stamp,
      'DTSTART:' + d8 + 'T090000', 'DTEND:' + d8 + 'T093000',
      'RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=1',
      'SUMMARY:Köp Top 5 (månadsköp)',
      'DESCRIPTION:Öppna Screener och fliken TOP 5. Köp lika mycket i var och en av de fem aktierna och bocka av.',
      'BEGIN:VALARM', 'ACTION:DISPLAY', 'DESCRIPTION:Köp Top 5', 'TRIGGER:PT0S', 'END:VALARM',
      'END:VEVENT', 'END:VCALENDAR'
    ];
    var url = 'data:text/calendar;charset=utf-8,' + encodeURIComponent(lines.join('\r\n'));
    var ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
    if (ios) { window.location.href = url; return; }
    var a = document.createElement('a');
    a.href = url; a.download = 'top5-manadskop.ics';
    document.body.appendChild(a); a.click(); a.remove();
  }

  // ---------- händelser ----------
  function onPanelChange(e) {
    var t = e.target;
    if (t && t.matches && t.matches('input[data-tk]')) {
      var key = ym(new Date());
      var tk = t.getAttribute('data-tk');
      var p = (data.picks || []).filter(function (x) { return x.ticker === tk; })[0];
      if (!p) return;
      var bought = lsGet(K_BOUGHT, {});
      var monthRec = bought[key] || { avail: [], done: [], shares: {} };
      if (!monthRec.shares) monthRec.shares = {};
      var banked = lsGet(K_BANKED, {});
      var pot = banked[tk] || 0;
      var i = monthRec.done.indexOf(tk);

      if (t.checked && i < 0) {
        var qty = p.price > 0 ? Math.floor(pot / p.price) : 0;
        if (qty >= 1) {
          banked[tk] = pot - qty * p.price;
          monthRec.done.push(tk);
          monthRec.shares[tk] = qty;
        }
      } else if (!t.checked && i > -1) {
        var refund = (monthRec.shares[tk] || 0) * p.price;
        banked[tk] = pot + refund;
        monthRec.done.splice(i, 1);
        delete monthRec.shares[tk];
      }
      bought[key] = monthRec;
      lsSet(K_BANKED, banked);
      lsSet(K_BOUGHT, bought);
      renderMonth();
    } else if (t && t.id === 't5Amount') {
      var v = parseFloat(t.value);
      if (isFinite(v) && v >= 0) { lsSet(K_AMOUNT, v); renderMonth(); }
    }
  }
  function onPanelInput(e) {
    var t = e.target;
    if (t && t.id === 't5Amount' && data) {
      var v = parseFloat(t.value);
      if (!isFinite(v) || v < 0) return;
      var per = data.picks.length ? v / data.picks.length : 0;
      var lbl = document.getElementById('t5Per');
      if (lbl) lbl.textContent = '= ' + fmtInt(per) + ' kr per aktie och månad';
    }
  }
  function onPanelClick(e) {
    var b = e.target.closest ? e.target.closest('[data-act]') : null;
    if (b && b.getAttribute('data-act') === 'ics') downloadIcs();
  }

  function loadData() {
    fetch('top5.json?_=' + Date.now())
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(function (d) { data = d; loadError = null; renderMonth(); })
      .catch(function (err) { if (!data) { loadError = String(err && err.message || err); } renderMonth(); });
  }

  function enter() {
    active = true;
    var tabs = document.querySelectorAll('.tab');
    for (var i = 0; i < tabs.length; i++) tabs[i].classList.remove('active');
    tab.classList.add('active');
    dataPanel.style.display = 'none';
    if (summaryHost) summaryHost.style.display = 'none';
    panel.style.display = '';
    renderMonth();
    loadData();
    if (tab.scrollIntoView) tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function leave() {
    active = false;
    tab.classList.remove('active');
    panel.style.display = 'none';
    dataPanel.style.display = '';
    if (summaryHost) summaryHost.style.display = '';
  }

  function init() {
    var tabsRow = document.getElementById('tabsRow');
    dataPanel = document.getElementById('dataPanel');
    summaryHost = document.getElementById('summaryHost');
    if (!tabsRow || !dataPanel) return; // sidan ser inte ut som väntat – gör ingenting

    var others = Array.prototype.slice.call(tabsRow.querySelectorAll('.tab'));
    injectCss();

    tab = document.createElement('div');
    tab.className = 'tab';
    tab.textContent = 'TOP 5';
    tabsRow.appendChild(tab);

    panel = document.createElement('div');
    panel.className = 'panel';
    panel.id = 'top5Panel';
    panel.style.display = 'none';
    panel.innerHTML = '<div class="t5"><div id="t5Month"></div>' + STATIC_HTML + '</div>';
    dataPanel.parentNode.insertBefore(panel, dataPanel.nextSibling);

    tab.addEventListener('click', enter);
    others.forEach(function (el) { el.addEventListener('click', leave); });
    panel.addEventListener('change', onPanelChange);
    panel.addEventListener('input', onPanelInput);
    panel.addEventListener('click', onPanelClick);

    var gb = document.getElementById('guideBtn');
    if (gb) gb.addEventListener('click', function () { if (active) panel.style.display = 'none'; });
    var gbb = document.getElementById('guideBackBtn');
    if (gbb) gbb.addEventListener('click', function () {
      if (!active) return;
      dataPanel.style.display = 'none';
      if (summaryHost) summaryHost.style.display = 'none';
      panel.style.display = '';
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
