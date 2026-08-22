/* Biotech Radar — frontend
 *
 * Reads the JSON the pipeline commits into data/ and renders it. No build
 * step, no framework, no dependencies: this has to run from a bare GitHub
 * Pages URL for years without maintenance.
 */
(function () {
  "use strict";

  var STATE = { radar: null, movers: null, calendar: null, moverTab: "gainers_1d", sort: { key: "score", dir: -1 } };

  // ---------- formatting ----------
  function pct(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    return (v >= 0 ? "+" : "") + (v * 100).toFixed(digits === undefined ? 1 : digits) + "%";
  }
  function num(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    return Number(v).toFixed(digits === undefined ? 2 : digits);
  }
  function money(v) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    return "$" + Number(v).toFixed(2);
  }
  function cap(v) {
    if (!v || !isFinite(v)) return "–";
    if (v >= 1e12) return "$" + (v / 1e12).toFixed(1) + "T";
    if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
    return "$" + v.toFixed(0);
  }
  function signClass(v) {
    if (v === null || v === undefined || !isFinite(v)) return "mute";
    return v > 0 ? "pos" : v < 0 ? "neg" : "mute";
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function el(id) { return document.getElementById(id); }

  function relTime(iso) {
    if (!iso) return "–";
    var then = new Date(iso), mins = (Date.now() - then.getTime()) / 60000;
    if (!isFinite(mins)) return "–";
    if (mins < 90) return Math.max(Math.round(mins), 0) + " min ago";
    if (mins < 60 * 36) return Math.round(mins / 60) + " h ago";
    return Math.round(mins / 1440) + " d ago";
  }

  // ---------- the signature: factor forest plot ----------
  //
  // A diverging bar chart with a null line at zero, one row per factor,
  // deliberately borrowing the grammar of a forest plot. Bars right of the
  // line push the composite up, bars left push it down. The reader can see
  // at a glance whether a score is carried by one dominant factor or by
  // broad agreement — which matters, because a score of +1.2 driven entirely
  // by momentum is a much weaker claim than the same score built from five
  // independent factors pointing the same way.
  function forest(contributions, labels) {
    var entries = Object.keys(contributions).map(function (k) {
      return { key: k, label: (labels && labels[k]) || k, value: contributions[k] };
    });
    entries.sort(function (a, b) { return Math.abs(b.value) - Math.abs(a.value); });

    var max = 0;
    entries.forEach(function (e) { max = Math.max(max, Math.abs(e.value)); });
    max = Math.max(max, 0.25);

    var rows = entries.map(function (e) {
      var half = (Math.abs(e.value) / max) * 50;
      var side = e.value >= 0
        ? 'left:50%;width:' + half.toFixed(2) + '%'
        : 'right:50%;width:' + half.toFixed(2) + '%';
      return '<div class="forest-row">' +
        '<div class="forest-label">' + esc(e.label) + '</div>' +
        '<div class="forest-track"><div class="forest-null"></div>' +
        '<div class="forest-bar ' + (e.value >= 0 ? "pos" : "neg") + '" style="' + side + '"></div></div>' +
        '<div class="forest-val">' + (e.value >= 0 ? "+" : "") + e.value.toFixed(2) + '</div>' +
        '</div>';
    }).join("");

    return '<div class="forest" role="img" aria-label="Factor contributions to the composite score">' +
      rows +
      '<div class="forest-axis"><div></div><div class="forest-axis-inner">' +
      '<span>−' + max.toFixed(2) + '</span><span>0</span><span>+' + max.toFixed(2) + '</span>' +
      '</div><div></div></div></div>';
  }

  // ---------- catalyst horizon ----------
  //
  // Biotech is a calendar sector: when something happens matters as much as
  // what. This strip puts today at the left edge and plots each dated event
  // on a compressed scale out to a year, so the shape of the forward risk is
  // legible without reading any dates.
  function horizon(catalysts) {
    if (!catalysts || !catalysts.length) return "";
    var W = 100, H = 34, base = 22, span = 365;

    var ticks = catalysts.filter(function (c) {
      return c.days_out >= -10 && c.days_out <= span;
    }).map(function (c) {
      var x = Math.sqrt(Math.max(c.days_out, 0) / span) * (W - 4) + 2;
      var h = 6 + (c.phase_weight || 0.4) * 10;
      var phase = (c.phases || []).join("/") || "Trial";
      return '<line x1="' + x.toFixed(2) + '" y1="' + base + '" x2="' + x.toFixed(2) +
        '" y2="' + (base - h).toFixed(1) + '" stroke="var(--indigo)" stroke-width="1.2" ' +
        'vector-effect="non-scaling-stroke"><title>' + esc(phase + " · " + c.date + " · " + c.nct_id) +
        '</title></line>' +
        '<circle cx="' + x.toFixed(2) + '" cy="' + (base - h).toFixed(1) + '" r="1.5" fill="var(--indigo)" ' +
        'vector-effect="non-scaling-stroke"/>';
    }).join("");

    var marks = [30, 90, 180, 365].map(function (d) {
      var x = Math.sqrt(d / span) * (W - 4) + 2;
      return '<line x1="' + x.toFixed(2) + '" y1="' + base + '" x2="' + x.toFixed(2) + '" y2="' + (base + 3) +
        '" stroke="var(--rule-strong)" stroke-width="0.7" vector-effect="non-scaling-stroke"/>' +
        '<text x="' + x.toFixed(2) + '" y="' + (H - 1) + '" font-size="5" fill="var(--ink-faint)" ' +
        'text-anchor="middle" font-family="var(--font-data)">' + d + 'd</text>';
    }).join("");

    return '<div class="horizon"><div class="horizon-title">Catalyst horizon</div>' +
      '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" ' +
      'role="img" aria-label="Timeline of upcoming trial readouts">' +
      '<line x1="2" y1="' + base + '" x2="' + (W - 2) + '" y2="' + base +
      '" stroke="var(--rule)" stroke-width="0.8" vector-effect="non-scaling-stroke"/>' +
      '<line x1="2" y1="' + (base - 16) + '" x2="2" y2="' + (base + 3) +
      '" stroke="var(--ink)" stroke-width="1" vector-effect="non-scaling-stroke"/>' +
      marks + ticks + '</svg></div>';
  }

  // ---------- ranked card ----------
  function card(row, rank, labels) {
    var cls = row.override ? "is-flag" : row.bucket === "constructive" ? "is-up" : row.bucket === "deteriorating" ? "is-down" : "";
    var scoreCls = row.score > 0.75 ? "score-up" : row.score < -0.75 ? "score-down" : "score-flat";

    var badges = "";
    if (row.held) badges += '<span class="badge badge-held">Held</span>';
    if (row.override) badges += '<span class="badge badge-flag">Hard flag</span>';
    (row.tags || []).slice(0, 2).forEach(function (t) {
      badges += '<span class="badge badge-tag">' + esc(t) + '</span>';
    });

    var reasons = (row.reasons || []).map(function (r) {
      var warn = /hold|letter|terminat|suspend|did not|fail|offering|below|cash|slipped|lagging|down /i.test(r);
      return '<li class="' + (warn ? "warn" : "") + '">' + esc(r) + '</li>';
    }).join("");

    var metrics = [
      ["1d", pct(row.ret_1d)], ["1m", pct(row.ret_21d)], ["3m", pct(row.ret_63d)],
      ["vs XBI 3m", pct(row.rs_63d)], ["RSI", num(row.rsi14, 0)],
      ["σ ann", row.vol_ann ? (row.vol_ann * 100).toFixed(0) + "%" : "–"],
      ["mkt cap", cap(row.market_cap)],
      ["runway", row.runway_months === null || row.runway_months === undefined ? "–" :
        (row.runway_months >= 900 ? "cash +ve" : Math.round(row.runway_months) + "m")]
    ].map(function (m) {
      return "<span>" + esc(m[0]) + " <b>" + esc(m[1]) + "</b></span>";
    }).join("");

    var detail = "";
    if ((row.headlines && row.headlines.length) || (row.filings && row.filings.length) ||
        (row.trial_changes && row.trial_changes.length)) {
      var parts = "";
      if (row.trial_changes && row.trial_changes.length) {
        parts += "<p><b>Registry changes</b></p><ul>" + row.trial_changes.map(function (c) {
          var txt = c.type === "status"
            ? c.nct_id + ": " + String(c.from || "").replace(/_/g, " ").toLowerCase() + " → " + String(c.to || "").replace(/_/g, " ").toLowerCase()
            : c.nct_id + ": primary completion moved " + c.shift_days + " days";
          return "<li>" + esc(txt) + ' <span class="src">' + esc((c.title || "").slice(0, 90)) + "</span></li>";
        }).join("") + "</ul>";
      }
      if (row.headlines && row.headlines.length) {
        parts += "<p><b>Recent coverage</b></p><ul>" + row.headlines.map(function (h) {
          var link = h.url ? '<a href="' + esc(h.url) + '" target="_blank" rel="noopener">' + esc(h.title) + "</a>" : esc(h.title);
          return "<li>" + link + ' <span class="src">' + esc(h.source) + " · " + esc(h.published) + "</span></li>";
        }).join("") + "</ul>";
      }
      if (row.filings && row.filings.length) {
        parts += "<p><b>SEC filings</b></p><ul>" + row.filings.map(function (f) {
          return "<li><a href=" + '"' + esc(f.url) + '" target="_blank" rel="noopener">' + esc(f.form) +
            "</a> — " + esc(f.label) + ' <span class="src">' + esc(f.filed) + "</span></li>";
        }).join("") + "</ul>";
      }
      detail = '<details class="more"><summary>Sources and filings</summary><div class="more-body">' + parts + "</div></details>";
    }

    return '<article class="card ' + cls + '">' +
      '<div class="card-head">' +
        '<span class="rank">' + (rank < 10 ? "0" : "") + rank + "</span>" +
        '<span class="tick">' + esc(row.ticker) + "</span>" +
        '<span class="coname">' + esc(row.name) + "</span>" + badges +
        '<span class="px">' + money(row.price) + "</span>" +
        '<span class="chg ' + signClass(row.ret_1d) + '">' + pct(row.ret_1d) + "</span>" +
        '<span class="score-chip ' + scoreCls + '">' + (row.score >= 0 ? "+" : "") + num(row.score) + "</span>" +
      "</div>" +
      forest(row.contributions || {}, labels) +
      '<ul class="reasons">' + reasons + "</ul>" +
      '<div class="metrics">' + metrics + "</div>" +
      horizon(row.catalysts) + detail +
    "</article>";
  }

  // ---------- movers ----------
  function renderMovers() {
    var m = STATE.movers;
    if (!m) return;
    var rows = m[STATE.moverTab] || [];
    var isVol = STATE.moverTab === "unusual_volume";
    var isWeek = STATE.moverTab.indexOf("5d") > -1;

    el("movers").innerHTML = rows.length ? rows.map(function (r) {
      var v = isVol ? r.volume_z : (isWeek ? r.ret_5d : r.ret_1d);
      var text = isVol ? (v >= 0 ? "+" : "") + num(v, 1) + "σ" : pct(v);
      return '<div class="mover">' +
        '<span class="mover-tick">' + esc(r.ticker) + "</span>" +
        '<span class="mover-name">' + esc(r.name) + "</span>" +
        '<span class="mover-val ' + signClass(isVol ? 1 : v) + '">' + text + "</span></div>";
    }).join("") : '<div class="empty">No data for this view.</div>';

    Array.prototype.forEach.call(document.querySelectorAll("#mover-tabs .tab"), function (t) {
      t.setAttribute("aria-selected", String(t.dataset.tab === STATE.moverTab));
    });
  }

  // ---------- calendar ----------
  function renderCalendar() {
    var events = (STATE.calendar && STATE.calendar.events) || [];
    var near = events.filter(function (e) { return e.days_out >= 0 && e.days_out <= 120; }).slice(0, 60);
    if (!near.length) {
      el("calendar").innerHTML = '<div class="empty">No dated events in the next 120 days.</div>';
      return;
    }
    var html = "", month = "";
    near.forEach(function (e) {
      var d = new Date(e.date + "T00:00:00Z");
      var label = isFinite(d.getTime())
        ? d.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" })
        : "Undated";
      if (label !== month) { month = label; html += '<div class="cal-month">' + esc(month) + "</div>"; }
      var day = isFinite(d.getTime())
        ? d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: "UTC" })
        : e.date;
      html += '<div class="cal-row">' +
        '<span class="cal-date">' + esc(day) + (e.estimated ? " ~" : "") + "</span>" +
        '<span class="cal-tick">' + esc(e.ticker || "—") + "</span>" +
        '<span class="cal-detail" title="' + esc(e.detail) + '">' + esc(e.detail) + "</span>" +
        '<span class="cal-kind kind-' + esc(e.kind) + '">' + esc(e.label) + "</span></div>";
    });
    el("calendar").innerHTML = html;
  }

  // ---------- full table ----------
  var COLUMNS = [
    { key: "ticker", label: "Ticker", fmt: function (r) { return '<span class="cell-tick">' + esc(r.ticker) + "</span>"; }, text: true },
    { key: "score", label: "Score", fmt: function (r) { return '<span class="' + signClass(r.score) + '">' + num(r.score) + "</span>"; } },
    { key: "price", label: "Price", fmt: function (r) { return money(r.price); } },
    { key: "ret_1d", label: "1d", fmt: function (r) { return '<span class="' + signClass(r.ret_1d) + '">' + pct(r.ret_1d) + "</span>"; } },
    { key: "ret_21d", label: "1m", fmt: function (r) { return '<span class="' + signClass(r.ret_21d) + '">' + pct(r.ret_21d, 0) + "</span>"; } },
    { key: "ret_63d", label: "3m", fmt: function (r) { return '<span class="' + signClass(r.ret_63d) + '">' + pct(r.ret_63d, 0) + "</span>"; } },
    { key: "rs_63d", label: "vs XBI", fmt: function (r) { return '<span class="' + signClass(r.rs_63d) + '">' + pct(r.rs_63d, 0) + "</span>"; } },
    { key: "rsi14", label: "RSI", fmt: function (r) { return num(r.rsi14, 0); } },
    { key: "vol_ann", label: "σ", fmt: function (r) { return r.vol_ann ? (r.vol_ann * 100).toFixed(0) + "%" : "–"; } },
    { key: "drawdown_52w", label: "From high", fmt: function (r) { return '<span class="' + signClass(r.drawdown_52w) + '">' + pct(r.drawdown_52w, 0) + "</span>"; } },
    { key: "gap_events", label: "Gaps", fmt: function (r) { return r.gap_events === null || r.gap_events === undefined ? "–" : r.gap_events; } },
    { key: "runway_months", label: "Runway", fmt: function (r) { return r.runway_months === null || r.runway_months === undefined ? "–" : (r.runway_months >= 900 ? "—" : Math.round(r.runway_months) + "m"); } },
    { key: "market_cap", label: "Mkt cap", fmt: function (r) { return cap(r.market_cap); } }
  ];

  function renderTable() {
    var rows = (STATE.radar && STATE.radar.rows) || [];
    var s = STATE.sort;
    var sorted = rows.slice().sort(function (a, b) {
      var x = a[s.key], y = b[s.key];
      if (typeof x === "string" || typeof y === "string") {
        return String(x || "").localeCompare(String(y || "")) * s.dir;
      }
      var xv = (x === null || x === undefined || !isFinite(x)) ? -Infinity : x;
      var yv = (y === null || y === undefined || !isFinite(y)) ? -Infinity : y;
      return (xv - yv) * s.dir;
    });

    el("table-head").innerHTML = "<tr>" + COLUMNS.map(function (c) {
      var sortAttr = s.key === c.key ? (s.dir === -1 ? "descending" : "ascending") : "none";
      return '<th data-key="' + c.key + '" aria-sort="' + sortAttr + '" tabindex="0" role="columnheader">' + esc(c.label) + "</th>";
    }).join("") + "</tr>";

    el("table-body").innerHTML = sorted.map(function (r) {
      return "<tr>" + COLUMNS.map(function (c) { return "<td>" + c.fmt(r) + "</td>"; }).join("") + "</tr>";
    }).join("");

    Array.prototype.forEach.call(el("table-head").querySelectorAll("th"), function (th) {
      function go() {
        var k = th.dataset.key;
        STATE.sort = { key: k, dir: STATE.sort.key === k ? -STATE.sort.dir : -1 };
        renderTable();
      }
      th.addEventListener("click", go);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
      });
    });
  }

  // ---------- hard flags ----------
  function renderFlags() {
    var rows = (STATE.radar && STATE.radar.rows) || [];
    var flagged = rows.filter(function (r) { return r.override === "hard_flag"; });
    if (!flagged.length) {
      el("flags").innerHTML = '<div class="empty">No thesis-ending events detected in the last 21 days.</div>';
      el("flag-count").textContent = "";
      return;
    }
    el("flag-count").textContent = flagged.length + " affected";
    el("flags").innerHTML = flagged.map(function (r) {
      var items = (r.hard_flags || []).map(function (f) {
        var link = f.url ? '<a href="' + esc(f.url) + '" target="_blank" rel="noopener">' + esc(f.title) + "</a>" : esc(f.title);
        return "<li>" + link + ' <span class="src">' + esc(f.source) + " · " + esc(f.published) + "</span></li>";
      });
      (r.trial_changes || []).filter(function (c) { return c.hard; }).forEach(function (c) {
        items.push("<li>" + esc(c.nct_id + " → " + String(c.to || "").replace(/_/g, " ").toLowerCase()) +
          ' <span class="src">' + esc((c.title || "").slice(0, 100)) + "</span></li>");
      });
      return '<article class="card is-flag">' +
        '<div class="card-head"><span class="tick">' + esc(r.ticker) + "</span>" +
        '<span class="coname">' + esc(r.name) + "</span>" +
        (r.held ? '<span class="badge badge-held">Held</span>' : "") +
        '<span class="chg ' + signClass(r.ret_5d) + '">' + pct(r.ret_5d) + " 5d</span></div>" +
        '<div class="more-body"><ul>' + items.join("") + "</ul></div></article>";
    }).join("");
  }

  // ---------- masthead ----------
  function renderHeader() {
    var d = STATE.radar;
    if (!d) return;
    el("stamp").innerHTML = "Data as of " + esc(new Date(d.as_of).toLocaleString("en-GB", { timeZone: "UTC" })) +
      " UTC<br>" + esc(relTime(d.as_of)) + " · " + d.ranked + " of " + d.universe_size + " names ranked";

    var bench = d.benchmarks || {};
    el("bench").innerHTML = Object.keys(bench).map(function (k) {
      var b = bench[k];
      return '<span class="bench-item"><b>' + esc(k) + "</b>" +
        '<span class="' + signClass(b.ret_1d) + '">' + pct(b.ret_1d) + "</span>" +
        '<span class="mute">3m ' + pct(b.ret_63d, 0) + "</span></span>";
    }).join("");

    if (d.demo) {
      // A quiet notice is not enough here. Every ticker on this page is a real
      // listed company, but every number attached to it was generated by a
      // random walk. Someone landing mid-scroll must not be able to mistake
      // this for a live board, so the demo state is marked on the whole page.
      document.body.setAttribute("data-demo", "1");
      document.title = "[SYNTHETIC DATA] " + document.title;
      el("demo-banner").innerHTML =
        '<div class="demo-bar" role="alert">' +
          '<span class="demo-bar-tag">Synthetic data</span>' +
          '<span class="demo-bar-text">Every price, headline, trial record and score on this page was ' +
          'randomly generated offline to exercise the interface. The tickers are real companies; ' +
          '<strong>none of the numbers describe them.</strong> Do not read this as a market view. ' +
          'Run <code>python scripts/build.py</code> to populate the board with live data.</span>' +
        "</div>";
    } else {
      document.body.removeAttribute("data-demo");
    }
  }

  // ---------- boot ----------
  function renderBuckets() {
    var d = STATE.radar, labels = d.labels || {};
    var up = (d.buckets && d.buckets.constructive) || [];
    var down = (d.buckets && d.buckets.deteriorating) || [];

    el("constructive-count").textContent = up.length + " of " + d.ranked;
    el("deteriorating-count").textContent = down.length + " of " + d.ranked;

    el("constructive").innerHTML = up.length
      ? up.map(function (r, i) { return card(r, i + 1, labels); }).join("")
      : '<div class="empty">No name currently clears the constructive threshold. That is a normal reading, not a failure — in a weak tape the honest answer is that nothing stands out.</div>';

    el("deteriorating").innerHTML = down.length
      ? down.map(function (r, i) { return card(r, i + 1, labels); }).join("")
      : '<div class="empty">Nothing is currently below the deterioration threshold.</div>';

    Array.prototype.forEach.call(document.querySelectorAll(".card"), function (c, i) {
      c.style.animationDelay = Math.min(i * 22, 400) + "ms";
    });
  }

  function fail(message) {
    el("constructive").innerHTML = '<div class="empty">' + esc(message) + "</div>";
  }

  function load() {
    var stamp = "?v=" + Date.now();
    Promise.all([
      fetch("data/radar.json" + stamp).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }),
      fetch("data/movers.json" + stamp).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
      fetch("data/calendar.json" + stamp).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
    ]).then(function (parts) {
      STATE.radar = parts[0];
      STATE.movers = parts[1];
      STATE.calendar = parts[2];
      renderHeader();
      renderMovers();
      renderBuckets();
      renderFlags();
      renderCalendar();
      renderTable();
    }).catch(function (err) {
      fail("Could not load data/radar.json (" + err.message + "). Run the pipeline first: " +
           "python scripts/build.py --demo");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("#mover-tabs .tab"), function (t) {
      t.addEventListener("click", function () { STATE.moverTab = t.dataset.tab; renderMovers(); });
    });
    load();
  });
})();
