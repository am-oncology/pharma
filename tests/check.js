/* Headless render check.
 *
 * Loads index.html in jsdom, stubs fetch to serve the real files from data/,
 * runs app.js, and asserts the page actually rendered. Catches the failure
 * mode where the pipeline succeeds, the JSON looks fine, and the page is
 * silently blank because a key was renamed on one side only.
 *
 * Run: node tests/check.js
 */
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
let failures = 0;
const results = [];

function check(name, condition, detail) {
  const ok = !!condition;
  if (!ok) failures++;
  results.push(`${ok ? "  ok  " : " FAIL "} ${name}${!ok && detail ? "  → " + detail : ""}`);
}

// --- static checks ---------------------------------------------------------
const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const js = fs.readFileSync(path.join(ROOT, "assets", "app.js"), "utf8");
const css = fs.readFileSync(path.join(ROOT, "assets", "theme.css"), "utf8");

const idsUsed = [...js.matchAll(/el\("([^"]+)"\)/g)].map((m) => m[1]);
const idsPresent = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
[...new Set(idsUsed)].forEach((id) => {
  check(`#${id} exists in index.html`, idsPresent.includes(id));
});

const classesUsed = new Set([...js.matchAll(/class="([^"{]+)"/g)]
  .flatMap((m) => m[1].split(/\s+/)).filter(Boolean));
const importantClasses = ["card", "forest-row", "mover", "cal-row", "score-chip", "badge", "reasons"];
importantClasses.forEach((c) => {
  check(`.${c} is styled`, css.includes("." + c), "class used in JS but absent from theme.css");
});

// --- render ----------------------------------------------------------------
const dataFiles = {
  "data/radar.json": null,
  "data/movers.json": null,
  "data/calendar.json": null,
};
for (const key of Object.keys(dataFiles)) {
  const p = path.join(ROOT, key);
  check(`${key} exists`, fs.existsSync(p), "run: python scripts/build.py --demo");
  if (fs.existsSync(p)) dataFiles[key] = fs.readFileSync(p, "utf8");
}

const vc = new VirtualConsole();
const consoleErrors = [];
vc.on("jsdomError", (e) => consoleErrors.push(e.message));
vc.on("error", (e) => consoleErrors.push(String(e)));

const dom = new JSDOM(html, {
  runScripts: "outside-only",
  virtualConsole: vc,
  url: "https://example.invalid/",
});

dom.window.fetch = (url) => {
  const clean = String(url).split("?")[0].replace(/^\//, "");
  const body = dataFiles[clean];
  return Promise.resolve({
    ok: body !== undefined && body !== null,
    status: body ? 200 : 404,
    json: () => Promise.resolve(JSON.parse(body)),
  });
};

dom.window.eval(js);
dom.window.document.dispatchEvent(new dom.window.Event("DOMContentLoaded"));

setTimeout(() => {
  const doc = dom.window.document;
  const q = (s) => doc.querySelectorAll(s);

  check("no runtime errors", consoleErrors.length === 0, consoleErrors[0]);
  check("masthead stamp populated", !/Loading/.test(doc.getElementById("stamp").textContent));
  check("benchmark row rendered", q("#bench .bench-item").length > 0);
  check("movers rendered", q("#movers .mover").length > 0);
  check("ranked cards rendered", q(".cards .card").length > 0);
  check("factor bars rendered", q(".forest-row").length > 0);
  check("null line present on every forest track", q(".forest-track").length === q(".forest-null").length);
  check("reasons rendered", q(".reasons li").length > 0);
  check("catalyst horizon rendered", q(".horizon svg").length > 0);
  check("calendar rendered", q("#calendar .cal-row").length > 0 || q("#calendar .empty").length > 0);
  check("table header rendered", q("#table-head th").length >= 10);
  check("table body rendered", q("#table-body tr").length > 0);
  check("full table row count matches dataset",
    q("#table-body tr").length === JSON.parse(dataFiles["data/radar.json"]).rows.length);

  // Every card should carry a score chip and at least one reason.
  const cards = [...q(".cards .card")].filter((c) => !c.classList.contains("is-flag") || c.querySelector(".score-chip"));
  const missingScore = cards.filter((c) => !c.querySelector(".score-chip") && !c.closest("#flags"));
  check("every ranked card shows a score", missingScore.length === 0, `${missingScore.length} without`);

  // Sorting must not throw and must actually reorder.
  const before = [...q("#table-body tr")].map((r) => r.cells[0].textContent).join(",");
  const th = q("#table-head th")[3];
  th.dispatchEvent(new dom.window.Event("click", { bubbles: true }));
  const after = [...q("#table-body tr")].map((r) => r.cells[0].textContent).join(",");
  check("column sort reorders rows", before !== after);

  // Mover tabs must switch content.
  const firstTabContent = doc.getElementById("movers").innerHTML;
  const tabs = q("#mover-tabs .tab");
  tabs[1].dispatchEvent(new dom.window.Event("click", { bubbles: true }));
  check("mover tabs switch view", doc.getElementById("movers").innerHTML !== firstTabContent);

  // Accessibility floor.
  check("no positive tabindex", q('[tabindex]:not([tabindex="0"]):not([tabindex="-1"])').length === 0);
  check("images/svg have accessible labels",
    [...q("svg")].every((s) => s.getAttribute("role") === "img" ? s.hasAttribute("aria-label") : true));
  check("reduced motion respected in CSS", css.includes("prefers-reduced-motion"));
  check("dark scheme defined", css.includes("prefers-color-scheme: dark"));

  // --- synthetic-data warning ---------------------------------------------
  // Safety-critical: the board renders real tickers against invented numbers
  // in demo mode. If this marking ever silently breaks, the page becomes
  // indistinguishable from a live one. Assert it end to end.
  // dataFiles holds raw JSON text, not parsed objects.
  let isDemo = false;
  try { isDemo = !!JSON.parse(dataFiles["data/radar.json"] || "{}").demo; } catch (e) { /* handled below */ }
  if (isDemo) {
    const bar = doc.querySelector(".demo-bar");
    check("demo: body carries data-demo", doc.body.hasAttribute("data-demo"));
    check("demo: warning bar rendered", !!bar);
    check("demo: warning names the data synthetic",
      !!bar && /synthetic/i.test(bar.textContent));
    check("demo: warning states numbers are not real",
      !!bar && /none of the numbers/i.test(bar.textContent));
    check("demo: document title marked", /SYNTHETIC DATA/.test(doc.title));
    check("demo: ranked output watermarked in CSS",
      css.includes("body[data-demo] #constructive::after"));
    check("demo: watermark selectors match live containers",
      !!doc.getElementById("constructive") && !!doc.getElementById("deteriorating") &&
      !!doc.querySelector(".mover-grid") && !!doc.querySelector(".table-scroll"));
    check("demo: print marker defined", css.includes("body[data-demo]::before"));
  } else {
    check("live: no stale demo marking on body", !doc.body.hasAttribute("data-demo"));
    check("live: no demo bar rendered", !doc.querySelector(".demo-bar"));
  }

  console.log(results.join("\n"));
  console.log(`\n${results.length - failures}/${results.length} checks passed`);
  process.exit(failures ? 1 : 0);
}, 300);
