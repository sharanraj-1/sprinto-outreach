// Builds docs/components-map.html: a swimlane Components Map (rows = actors/services, columns = stages).
// Same visual system as the YouTube Trend Radar map. Render to PNG/PDF with headless Edge/Chrome.
// Usage: node scripts/build-components-map.mjs [--page <path>]
//   --page <path>  also writes a body-only, responsive copy (for publishing as a hosted page)
import { writeFileSync } from "node:fs";

const W = 1230;
const C = {
  title: "#7f938a",
  text: "#1f2622",
  body: "#4f5b55",
  label: "#7f938a",
  grid: "#c6cfca",
  gridStrong: "#8fa39a",
  card: "#6f837a",
  cardText: "#7a8a83",
  mono: "#29302c",
  arrow: "#8fa39a",
  cut: "#c0492f",
  cutBorder: "#e7a796",
};

// ---------------- layout ----------------
const LANE_X = 30;
const COL0 = 168;
const COL_W = 200;
const COLS = ["TRIGGER", "INPUT", "CONTEXT", "PROCESSING", "OUTPUT"];
const LANE_TOP = 185;
const LANE_H = 110;
const CARD_W = 162;

const LANES = [
  ["SHARAN", "Growth, Sprinto"],
  ["RESEND", "email to self"],
  ["COMPANY WEBSITE", "public pages"],
  ["HUNTER · CSV", "contact data"],
  ["OUTREACH APP", "FastAPI on Vercel"],
  ["GEMINI FLASH", "via OpenRouter"],
];

// [lane, col, text lines, mono detail]
const CARDS = {
  shTrig: [0, 0, ["Has a list of SaaS", "companies to prospect"], "paste domains · Run"],
  shOut: [0, 4, ["Reads the draft in his", "inbox, then decides", "whether to reach out"], "→ he writes to them"],
  rsOut: [1, 4, ["Sends the approved", "draft to his own inbox,", "with a review footer"], "resend.dev · SEND_TO only"],
  webIn: [2, 1, ["Homepage HTML, scripts,", "CSS, sitemap and", "job board"], "robots.txt · 1.5 s delay"],
  webCtx: [2, 2, ["Named pages it asks for:", "/trust, /security,", "/careers, /pricing"], "≤ 4 fetches + 2 to verify"],
  ctProc: [3, 3, ["Hand-picked CSV, then", "Hunter, then names on", "their own team page"], "email confidence ≥ 50"],
  appTrig: [4, 0, ["One request per company", "runs all seven steps", "and returns a card"], "POST /api/run · ≤ 300 s"],
  appIn: [4, 1, ["Deep-scans the site:", "tools in its code, cert", "badges, open roles"], "Vanta/Drata = certified"],
  appCtx: [4, 2, ["Checks the Evidence Map:", "B2B SaaS? Certified?", "Right stage?"], "not B2B SaaS → stop"],
  appProc: [4, 3, ["Applies the fit bar and", "ranks who to write to,", "in plain code"], "fit ≥ 3/5 · sec → CTO → CEO"],
  appOut: [4, 4, ["Checks the draft in code,", "then shows the postcard", "and editable email"], "70-120 words · 1 question"],
  gmIn: [5, 1, ["Fills named fields from", "each page, null when", "the page doesn't say"], "facts keep their page"],
  gmCtx: [5, 2, ["Names the business model", "and quotes the phrase", "that decided it"], "no quote, no answer"],
  gmProc: [5, 3, ["Scores fit 1-5, says", "why now, and finds", "two pain points"], "code applies the bar"],
  gmOut: [5, 4, ["Drafts the email from a", "real fact on their site;", "redrafts once if needed"], "gemini-3.8-flash"],
};

const laneY = (i) => LANE_TOP + i * LANE_H;
const colX = (i) => COL0 + i * COL_W;
const GRID_BOTTOM = laneY(LANES.length);

function cardBox(key) {
  const [lane, col, lines] = CARDS[key];
  const h = 18 + lines.length * 14.5 + 20;
  const x = colX(col) + (COL_W - CARD_W) / 2;
  const y = laneY(lane) + (LANE_H - h) / 2;
  return { x, y, w: CARD_W, h, cx: x + CARD_W / 2, cy: y + h / 2, right: x + CARD_W, bottom: y + h };
}

const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// ---------------- svg pieces ----------------
let svg = "";

// column headers
COLS.forEach((name, i) => {
  const cx = colX(i) + COL_W / 2;
  const half = name.length * 5.4 + 12;
  svg += `<line x1="${colX(i) + 8}" y1="167" x2="${cx - half}" y2="167" stroke="${C.grid}" stroke-dasharray="1 2"/>`;
  svg += `<line x1="${cx + half}" y1="167" x2="${colX(i) + COL_W - 8}" y2="167" stroke="${C.grid}" stroke-dasharray="1 2"/>`;
  svg += `<text x="${cx}" y="171" text-anchor="middle" class="col">${name.split("").join(" ")}</text>`;
});

// lanes + grid
svg += `<line x1="${LANE_X}" y1="${LANE_TOP}" x2="${colX(5)}" y2="${LANE_TOP}" stroke="${C.gridStrong}"/>`;
LANES.forEach(([name, sub], i) => {
  const y = laneY(i);
  svg += `<line x1="${LANE_X}" y1="${y + LANE_H}" x2="${colX(5)}" y2="${y + LANE_H}" stroke="${i === LANES.length - 1 ? C.gridStrong : C.grid}"/>`;
  svg += `<text x="${(LANE_X + COL0) / 2}" y="${y + LANE_H / 2 - 3}" text-anchor="middle" class="lane">${esc(name)}</text>`;
  svg += `<text x="${(LANE_X + COL0) / 2}" y="${y + LANE_H / 2 + 14}" text-anchor="middle" class="sub">${esc(sub)}</text>`;
});
for (let i = 0; i <= COLS.length; i++) {
  const x = colX(i);
  svg += `<line x1="${x}" y1="${LANE_TOP}" x2="${x}" y2="${GRID_BOTTOM}" stroke="${C.grid}"/>`;
}
svg += `<line x1="${LANE_X}" y1="${LANE_TOP}" x2="${LANE_X}" y2="${GRID_BOTTOM}" stroke="${C.gridStrong}"/>`;

// arrows (drawn before cards so cards sit on top)
function path(points) {
  const d = points.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [sx, sy] = points[0];
  return `<path d="${d}" fill="none" stroke="${C.arrow}" stroke-width="1.2" stroke-dasharray="2 3" marker-end="url(#arrow)"/><circle cx="${sx}" cy="${sy}" r="2.6" fill="${C.arrow}"/>`;
}
const B = (k) => cardBox(k);
const right = (k) => [B(k).right, B(k).cy];
const left = (k) => [B(k).x - 2, B(k).cy];
const top = (k) => [B(k).cx, B(k).y - 2];
const bottom = (k) => [B(k).cx, B(k).bottom];
const upInto = (k) => [B(k).cx, B(k).bottom + 2];

const arrows = [
  // Sharan pastes domains; each one becomes a request to the app
  [bottom("shTrig"), top("appTrig")],
  [right("appTrig"), left("appIn")],
  // the homepage and its layers feed the deep scan
  [bottom("webIn"), top("appIn")],
  // page text goes to Gemini for named fields, then the model-classified answers come back
  [bottom("appIn"), top("gmIn")],
  [right("gmIn"), left("gmCtx")],
  [top("gmCtx"), upInto("appCtx")],
  [right("appIn"), left("appCtx")],
  // open Evidence Map questions fetch the named page for each
  [bottom("webCtx"), top("appCtx")],
  [right("appCtx"), left("appProc")],
  // contacts come down from the CSV / Hunter / team-page lane
  [bottom("ctProc"), top("appProc")],
  // fit score and pain points are the model's call; the bar and ranking are code
  [bottom("appProc"), top("gmProc")],
  [right("appProc"), left("appOut")],
  [right("gmProc"), left("gmOut")],
  // the draft comes back up to be checked in code
  [top("gmOut"), upInto("appOut")],
  // approved draft goes out through Resend, to Sharan's own inbox
  [top("appOut"), upInto("rsOut")],
  [top("rsOut"), upInto("shOut")],
];
arrows.forEach((pts) => (svg += path(pts)));

// cards
for (const key of Object.keys(CARDS)) {
  const [, , lines, mono] = CARDS[key];
  const b = cardBox(key);
  svg += `<rect x="${b.x}" y="${b.y}" width="${b.w}" height="${b.h}" fill="#fff" stroke="${C.card}" stroke-width="0.9"/>`;
  svg += `<line x1="${b.x}" y1="${b.bottom}" x2="${b.right}" y2="${b.bottom}" stroke="${C.card}" stroke-width="2"/>`;
  lines.forEach((ln, i) => {
    svg += `<text x="${b.cx}" y="${b.y + 18 + i * 14.5}" text-anchor="middle" class="card">${esc(ln)}</text>`;
  });
  svg += `<text x="${b.cx}" y="${b.y + 18 + lines.length * 14.5 + 6}" text-anchor="middle" class="mono">${esc(mono)}</text>`;
}

// ---------------- shared map markup ----------------
const H = GRID_BOTTOM + 30 + 160 + 62;
const FONTS = `<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=JetBrains+Mono:wght@500&display=swap">`;
const MAP_CSS = `
  .map { position: relative; width: ${W}px; height: ${H}px; background: #fff; color: ${C.text}; font-family: Inter, "Segoe UI", Helvetica, Arial, sans-serif; }
  .map .head { position: absolute; left: 30px; top: 30px; width: 880px; }
  .map h1 { margin: 0; font-size: 33px; font-weight: 600; color: ${C.title}; letter-spacing: -0.2px; line-height: 1.2; }
  .map .lede { margin: 12px 0 0; font-size: 15px; line-height: 1.55; color: #34403a; }
  .map svg { position: absolute; left: 0; top: 0; }
  .map svg text.col { font-size: 10.5px; letter-spacing: 1.5px; fill: ${C.label}; }
  .map svg text.lane { font-size: 12.5px; font-weight: 600; letter-spacing: 0.6px; fill: ${C.label}; }
  .map svg text.sub { font-size: 11.5px; fill: ${C.text}; }
  .map svg text.card { font-size: 11.3px; fill: ${C.cardText}; }
  .map svg text.mono { font-family: "JetBrains Mono", "Cascadia Mono", Consolas, Menlo, monospace; font-size: 9.4px; fill: ${C.mono}; }
  .map .notes { position: absolute; left: 30px; top: ${GRID_BOTTOM + 30}px; width: ${colX(5) - 30}px; display: grid; grid-template-columns: 382px 1fr 1fr; gap: 30px; }
  .map .cut { border: 1px solid ${C.cutBorder}; border-radius: 8px; padding: 16px 16px 14px; }
  .map .cut h3 { color: ${C.cut}; }
  .map h3 { margin: 0 0 9px; font-size: 14.5px; font-weight: 600; color: ${C.label}; }
  .map .cut p { margin: 0; font-size: 12px; line-height: 1.6; color: #34403a; }
  .map ul { margin: 0; padding-left: 15px; font-size: 12px; line-height: 1.75; color: #34403a; }
  .map .foot { position: absolute; left: 30px; right: ${W - colX(5)}px; top: ${H - 52}px; border-top: 1px solid #e3e7e4; padding-top: 12px; display: flex; justify-content: space-between; font-size: 11.5px; color: #4f5b55; }
  .map .foot b { color: ${C.text}; font-weight: 600; }`;

const MAP = `<div class="map">
  <div class="head">
    <h1>Sprinto Outreach · Components Map</h1>
    <p class="lede">Paste a company's domain: the pipeline reads its site, checks the fit for Sprinto, finds the right person and drafts a cold email in about a minute. Gemini Flash writes the words; plain code decides.</p>
  </div>
  <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Swimlane map of the Sprinto Outreach pipeline">
    <defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L8,4 L0,8 z" fill="${C.arrow}"/></marker></defs>
    ${svg}
  </svg>
  <div class="notes">
    <div class="cut">
      <h3>The Cut</h3>
      <p>The AI never decides who gets an email. Gemini fills in facts, quotes the page and writes the words; plain code applies the Evidence Map, the fit bar and the contact ranking, and checks every draft. Nothing reaches a prospect: each email lands in Sharan's own inbox first.</p>
    </div>
    <div>
      <h3>Humans stay here</h3>
      <ul>
        <li>Sharan picks which companies to research</li>
        <li>He reads and edits every draft before it goes out</li>
        <li>Every send goes to his own inbox, never to the prospect</li>
        <li>A guessed email address is flagged for him to check</li>
      </ul>
    </div>
    <div>
      <h3>When something fails</h3>
      <ul>
        <li>Not B2B SaaS? Stops after the homepage, no contact search</li>
        <li>Model rate limit? Waits and retries up to 4×</li>
        <li>No confident contact? Marked "needs a contact" for a person</li>
        <li>Draft fails a check? Redrafted once, then flagged</li>
      </ul>
    </div>
  </div>
  <div class="foot">
    <span><b>Sprinto Outreach</b> · company domain → checked fit → reviewed cold email</span>
    <span><b>Live:</b> sprinto-outreach-three.vercel.app</span>
  </div>
</div>`;

// ---------------- standalone file (fixed size, for PNG/PDF) ----------------
const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Sprinto Outreach · Components Map</title>
${FONTS}
<style>
  @page { size: ${W}px ${H}px; margin: 0; }
  html, body { margin: 0; background: #fff; }
  ${MAP_CSS}
</style></head>
<body>
${MAP}
</body></html>`;
writeFileSync(new URL("../docs/components-map.html", import.meta.url), html);
console.log(`wrote docs/components-map.html (${W}x${H})`);

// ---------------- hosted page (body only, scrolls sideways on small screens) ----------------
const pageArg = process.argv.indexOf("--page");
if (pageArg > -1 && process.argv[pageArg + 1]) {
  // A single deliberate light look (it is a print-style diagram), so colours are set explicitly.
  const page = `<title>Sprinto Outreach Components Map</title>
<meta name="claude:viewport-fit-sides" content="cover">
${FONTS}
<style>
  :root { --bg: #fff; --fg: ${C.text}; --hint: ${C.label}; color-scheme: light; }
  body { background: var(--bg); color: var(--fg); padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 16px; }
  .scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  .scroll .map { margin: 0 auto; }
  .hint { display: none; font: 13px Inter, "Segoe UI", sans-serif; color: var(--hint); margin: 0 0 8px; }
  @media (max-width: 1260px) { .hint { display: block; } }
  ${MAP_CSS}
</style>
<p class="hint">The map is ${W}px wide. Scroll sideways to see every column.</p>
<div class="scroll">
${MAP}
</div>`;
  writeFileSync(process.argv[pageArg + 1], page);
  console.log(`wrote ${process.argv[pageArg + 1]}`);
}
