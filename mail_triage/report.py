"""Reports: a CSV and a single self-contained HTML page. Both stay in the private data folder.

The HTML page loads nothing from the internet (its Content-Security-Policy forbids it), so
opening it in a browser cannot leak anything. It is interactive: the decisions are embedded as
inert JSON and one inline script (allowed by its hash only) draws the overview bars, filters
and sorts the table. Mail data is only ever inserted with textContent, never as markup.
"""

from __future__ import annotations

import base64
from collections import Counter
import csv
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path

from .config import Config
from .privacy import csv_safe, private_dir, private_file
from .store import Store


ORDER = {"junk": 0, "review": 1, "keep": 2}
TITLES = {"junk": "Junk (highly likely)", "review": "To review", "keep": "For you"}
_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--soft:#f5f5f7;--accent:#2a78d6;
--junk:#c0392b;--review:#b7791f;--keep:#2f855a;
--src-model:#2a78d6;--src-memory:#eb6834;--src-rule:#1baf7a;--src-teach:#eda100}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f7;--mute:#a1a1a6;--line:#3a3a3c;--soft:#2c2c2e;
--accent:#3987e5;--junk:#e5534b;--review:#c69026;--keep:#57ab5a;
--src-model:#3987e5;--src-memory:#d95926;--src-rule:#199e70;--src-teach:#c98500}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:14px -apple-system,Segoe UI,sans-serif;margin:24px auto;max-width:1240px;padding:0 16px}
h1{font-size:22px;margin-bottom:4px}h2{font-size:17px;margin:28px 0 10px}.mute{color:var(--mute)}
code{font-size:13px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}
.card{border:1px solid var(--line);border-radius:10px;padding:10px 16px;background:none;color:inherit;font:inherit;
text-align:left;cursor:pointer;transition:border-color .15s,box-shadow .15s}
.card:hover,.card:focus-visible{border-color:var(--accent);outline:none}
.card.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.card b{font-size:22px;display:block}
.junk{color:var(--junk)}.review{color:var(--review)}.keep{color:var(--keep)}
.overview{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px 32px}
.viz h3{font-size:13px;font-weight:600;margin:0 0 8px;color:var(--mute)}
.viz h3 b{color:var(--fg);font-size:15px}
.bar{display:flex;gap:2px;height:34px}
.seg{border:0;padding:0 8px;min-width:4px;color:#fff;font:600 12px/34px -apple-system,Segoe UI,sans-serif;
text-align:left;white-space:nowrap;overflow:hidden;cursor:pointer;transition:opacity .15s,filter .15s}
.seg:first-child{border-radius:4px 0 0 4px}.seg:last-child{border-radius:0 4px 4px 0}.seg:only-child{border-radius:4px}
.seg:hover,.seg:focus-visible{filter:brightness(1.12);outline:2px solid var(--fg);outline-offset:1px}
.dim{opacity:.28}
.legend{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:8px;font-size:13px}
.legend button{background:none;border:0;color:inherit;font:inherit;padding:2px 0;cursor:pointer;display:flex;gap:6px;align-items:center}
.legend button:hover span:last-child{text-decoration:underline}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block}
.tip{position:fixed;pointer-events:none;background:var(--bg);border:1px solid var(--line);border-radius:6px;
padding:6px 10px;font-size:13px;box-shadow:0 4px 14px rgba(0,0,0,.18);z-index:5}
.tip b{display:block;font-size:15px}
.tools{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 10px;position:sticky;top:0;
background:var(--bg);padding:8px 0;z-index:2;border-bottom:1px solid var(--line)}
.tools input,.tools select{font:inherit;color:inherit;background:var(--soft);border:1px solid var(--line);
border-radius:6px;padding:6px 8px}
.tools input{flex:1 1 260px;min-width:200px}
.tools input:focus,.tools select:focus{outline:2px solid var(--accent);outline-offset:-1px}
.tools button{font:inherit;color:inherit;background:none;border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer}
.count{margin-left:auto;font-size:13px}
table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--mute);white-space:nowrap}
th button{all:unset;cursor:pointer;display:inline-flex;gap:4px;align-items:center}
th button:hover,th button:focus-visible{color:var(--fg)}
th button:focus-visible{outline:2px solid var(--accent)}
th .arrow{font-size:11px;opacity:.45}th[aria-sort] .arrow{opacity:1;color:var(--accent)}
tbody tr:hover{background:var(--soft)}
td.from{word-break:break-all}td.v{font-weight:600}
details summary{cursor:pointer}td.r{font-size:12px;color:var(--mute)}
.more{margin:12px 0;font:inherit;color:inherit;background:none;border:1px solid var(--line);border-radius:6px;padding:6px 14px;cursor:pointer}
.plan td:last-child{text-align:right;font-variant-numeric:tabular-nums}
.plan{max-width:520px}
"""
_JS = r"""
"use strict";
const DATA = JSON.parse(document.getElementById("mt-data").textContent);
const ROWS = DATA.rows;
const VERDICTS = [["keep", "For you"], ["review", "To review"], ["junk", "Junk"]];
const SOURCES = [["model", "AI model"], ["memory", "Sender memory"], ["rule", "Rule"], ["teach", "Teaching"]];
const LABEL = Object.fromEntries([...VERDICTS, ...SOURCES]);
const ORDER = { junk: 0, review: 1, keep: 2 };
const STEP = 500;
const state = { q: [], v: "", c: "", l: "", b: "", sort: "", dir: 1, shown: STEP };
const $ = (id) => document.getElementById(id);

for (const r of ROWS) r.text = `${r.f} ${r.s} ${r.c} ${r.m}`.toLowerCase();

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.append(child);   // strings become text nodes, never markup
  return node;
}

// -- filtering --------------------------------------------------------------------------
function sourceMatches(want, have) {
  return !want || (want === "-model" ? have !== "model" : have === want);
}
function matches(r, skip) {
  if (skip !== "v" && state.v && r.v !== state.v) return false;
  if (skip !== "b" && !sourceMatches(state.b, r.b)) return false;
  if (state.c && r.c !== state.c) return false;
  if (state.l && r.l !== state.l) return false;
  return state.q.every((term) => r.text.includes(term));
}
function set(key, value) {
  state[key] = state[key] === value ? "" : value;   // clicking the active choice clears it
  state.shown = STEP;
  const focused = document.activeElement && document.activeElement.dataset.id;
  update();
  // The bars were redrawn: keep keyboard focus on the same choice, drop the stale tooltip.
  tip.hidden = true;
  const again = focused && document.querySelector(`[data-id="${focused}"]`);
  if (again) again.focus();
}

// -- overview bars (each bar honours every filter except its own, so they cross-filter) ----
const tip = el("div", { class: "tip", hidden: "" });
document.body.append(tip);
function showTip(event, label, n, pct) {
  tip.replaceChildren(el("b", {}, `${n} · ${pct}%`), label);
  tip.hidden = false;
  const box = event.target.getBoundingClientRect();
  const x = event.clientX || box.left + box.width / 2;
  tip.style.left = `${Math.min(x + 12, innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${(event.clientY || box.top) - tip.offsetHeight - 10}px`;
}

function drawBar(id, key, items, colorOf) {
  const rows = ROWS.filter((r) => matches(r, key));
  const counts = Object.fromEntries(items.map(([value]) => [value, 0]));
  for (const r of rows) if (r[key] in counts) counts[r[key]] += 1;
  const total = rows.length || 1;
  const active = state[key];
  const bar = $(`${id}-bar`), legend = $(`${id}-legend`);
  bar.replaceChildren();
  legend.replaceChildren();
  for (const [value, label] of items) {
    const n = counts[value];
    const pct = n && n / total < 0.01 ? "<1" : String(Math.round((n / total) * 100));
    const dim = active && !sourceMatches(active, value) ? " dim" : "";
    const pick = () => set(key, value);
    if (n) {
      const seg = el("button", { class: `seg${dim}`, style: `flex:${n} 1 0;background:${colorOf(value)}`,
                                 "aria-label": `${label}: ${n} emails, ${pct}%`, "data-id": `seg-${key}-${value}`,
                                 onclick: pick });
      seg.dataset.labels = JSON.stringify([`${label} ${pct}%`, `${pct}%`]);
      seg.addEventListener("pointermove", (event) => showTip(event, label, n, pct));
      seg.addEventListener("focus", (event) => showTip(event, label, n, pct));
      seg.addEventListener("pointerleave", () => { tip.hidden = true; });
      seg.addEventListener("blur", () => { tip.hidden = true; });
      bar.append(seg);
    }
    legend.append(el("button", { class: dim.trim(), onclick: pick, "aria-pressed": String(active === value),
                                 "data-id": `legend-${key}-${value}` },
      el("i", { style: `background:${colorOf(value)}` }), el("span", {}, `${label} ${n} (${pct}%)`)));
  }
  fitLabels(bar);
  return { counts, total: rows.length };
}

// The longest label that fits inside each segment: "Junk 27%", else "27%", else none (legend has it).
function fitLabels(bar) {
  for (const seg of bar.children) {
    seg.textContent = "";
    for (const text of JSON.parse(seg.dataset.labels)) {
      seg.textContent = text;
      if (seg.scrollWidth <= seg.clientWidth) break;
      seg.textContent = "";
    }
  }
}
let resizing;
addEventListener("resize", () => {
  clearTimeout(resizing);
  resizing = setTimeout(() => { fitLabels($("verdict-bar")); fitLabels($("source-bar")); }, 100);
});

function drawOverview() {
  const verdict = drawBar("verdict", "v", VERDICTS, (v) => `var(--${v})`);
  const source = drawBar("source", "b", SOURCES, (b) => `var(--src-${b})`);
  const without = source.total - source.counts.model;
  $("verdict-head").textContent = `${verdict.total} emails`;
  $("source-head").textContent = `${source.total ? Math.round((without / source.total) * 100) : 0}% without the model`;
}

// -- table ----------------------------------------------------------------------------------
const COLUMNS = {
  d: (r) => r.d, f: (r) => r.f, s: (r) => r.s.toLowerCase(), v: (r) => ORDER[r.v],
  c: (r) => r.c, l: (r) => r.p, b: (r) => r.b,
};
function compare(a, b) {
  if (!state.sort) return ORDER[a.v] - ORDER[b.v] || b.p - a.p || (a.d < b.d ? 1 : -1);
  const key = COLUMNS[state.sort], x = key(a), y = key(b);
  return (x < y ? -1 : x > y ? 1 : 0) * state.dir || (a.d < b.d ? 1 : -1);
}
function row(r) {
  const why = el("details", {}, el("summary", {}, "show"));
  r.w.forEach((reason, i) => { if (i) why.append(el("br")); why.append(reason); });
  return el("tr", {},
    el("td", {}, r.d), el("td", { class: "from" }, r.f),
    el("td", {}, r.s || el("span", { class: "mute" }, "(not stored)")),
    el("td", { class: `v ${r.v}` }, LABEL[r.v]), el("td", {}, r.c), el("td", {}, r.l),
    el("td", {}, LABEL[r.b] || r.b), el("td", { class: "r" }, why));
}
function drawTable() {
  const rows = ROWS.filter((r) => matches(r)).sort(compare);
  const body = document.createDocumentFragment();
  for (const r of rows.slice(0, state.shown)) body.append(row(r));
  $("rows").replaceChildren(body);
  const rest = rows.length - Math.min(rows.length, state.shown);
  $("more").hidden = rest <= 0;
  $("more").textContent = `Show ${Math.min(rest, STEP)} more (${rest} not shown)`;
  $("count").textContent = rows.length === ROWS.length ? `${ROWS.length} emails`
    : `${rows.length} of ${ROWS.length} emails`;
  for (const th of document.querySelectorAll("th[data-key]")) {
    const on = th.dataset.key === state.sort;
    if (on) th.setAttribute("aria-sort", state.dir > 0 ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
    th.querySelector(".arrow").textContent = on ? (state.dir > 0 ? "▲" : "▼") : "↕";
  }
}

function update() {
  $("f-v").value = state.v; $("f-b").value = state.b; $("f-c").value = state.c; $("f-l").value = state.l;
  for (const card of document.querySelectorAll(".card[data-key]")) {
    const on = state[card.dataset.key] === card.dataset.value;
    card.classList.toggle("on", on);
    card.setAttribute("aria-pressed", String(on));
  }
  $("clear").hidden = !(state.q.length || state.v || state.b || state.c || state.l);
  drawOverview();
  drawTable();
}

// -- controls -------------------------------------------------------------------------------
function options(id, values, label = (v) => v) {
  const select = $(id);
  for (const value of values) select.append(el("option", { value }, label(value)));
}
options("f-v", VERDICTS.map(([v]) => v), (v) => LABEL[v]);
options("f-b", ["-model", ...SOURCES.map(([b]) => b)], (b) => b === "-model" ? "Without the model" : LABEL[b]);
options("f-c", [...new Set(ROWS.map((r) => r.c))].sort());
const BANDS = ["highly likely", "likely", "uncertain", "unlikely", "highly unlikely"];
const bands = new Set(ROWS.map((r) => r.l));
options("f-l", [...BANDS.filter((b) => bands.has(b)), ...[...bands].filter((b) => !BANDS.includes(b)).sort()]);
for (const [id, key] of [["f-v", "v"], ["f-b", "b"], ["f-c", "c"], ["f-l", "l"]]) {
  $(id).addEventListener("change", (event) => { state[key] = event.target.value; state.shown = STEP; update(); });
}
let typing;
$("q").addEventListener("input", (event) => {
  clearTimeout(typing);
  typing = setTimeout(() => {
    state.q = event.target.value.toLowerCase().split(/\s+/).filter(Boolean);
    state.shown = STEP;
    update();
  }, 120);
});
$("clear").addEventListener("click", () => {
  Object.assign(state, { q: [], v: "", c: "", l: "", b: "", shown: STEP });
  $("q").value = "";
  update();
});
$("more").addEventListener("click", () => { state.shown += STEP; drawTable(); });
for (const card of document.querySelectorAll(".card[data-key]")) {
  card.addEventListener("click", () => set(card.dataset.key, card.dataset.value));
}
for (const th of document.querySelectorAll("th[data-key]")) {
  th.querySelector("button").addEventListener("click", () => {
    // Click cycle per column: its natural direction (newest / most likely first), the opposite, default order.
    const key = th.dataset.key, first = key === "d" || key === "l" ? -1 : 1;
    if (state.sort !== key) { state.sort = key; state.dir = first; }
    else if (state.dir === first) state.dir = -first;
    else { state.sort = ""; state.dir = 1; }
    drawTable();
  });
}
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== $("q")) { event.preventDefault(); $("q").focus(); }
  if (event.key === "Escape" && document.activeElement === $("q")) { $("q").value = ""; $("q").dispatchEvent(new Event("input")); }
});
update();
"""
_JS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(_JS.encode()).digest()).decode()
CSP = f"default-src 'none'; style-src 'unsafe-inline'; script-src '{_JS_HASH}'"


def _rows(store: Store, accounts: list[str] | None):
    rows = store.rows(accounts)
    return sorted(rows, key=lambda r: (ORDER.get(r["verdict"], 3), -r["junk_probability"]))


def _json_for_html(value) -> str:
    """JSON that cannot close its <script> element or start markup, whatever the mail contains."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def write(store: Store, config: Config, folder: Path, accounts: list[str] | None = None) -> tuple[Path, Path]:
    private_dir(folder)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    rows = _rows(store, accounts)
    csv_path = private_file(folder / f"report-{stamp}.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["verdict", "category", "planned_folder", "likelihood", "junk_probability", "source",
                         "account", "mailbox", "received", "sender", "subject", "reasons"])
        for r in rows:
            writer.writerow([csv_safe(v) for v in (
                r["verdict"], r["category"], r["folder"], r["likelihood"], f"{r['junk_probability']:.4f}",
                r["source"], r["account"], r["mailbox"], r["received"], r["sender"], r["subject"],
                "; ".join(json.loads(r["reasons"])))])

    counts = Counter(r["verdict"] for r in rows)
    sources = Counter(r["source"] for r in rows)
    plan = Counter(r["folder"] for r in rows if r["folder"])
    data = {"rows": [{"d": r["received"][:10], "f": r["sender"], "s": r["subject"], "v": r["verdict"],
                      "c": r["category"], "l": r["likelihood"], "p": round(r["junk_probability"], 4),
                      "b": r["source"], "m": r["mailbox"], "w": json.loads(r["reasons"])} for r in rows]}
    e = html.escape
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        f"<meta http-equiv=Content-Security-Policy content=\"{CSP}\">",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>Mail Triage report</title><style>{_CSS}</style></head><body>",
        f"<h1>Mail Triage report</h1><p class=mute>{e(datetime.now().strftime('%Y-%m-%d %H:%M'))} · "
        f"local file, nothing was sent anywhere · settings {e(config.fingerprint())}</p><div class=cards>",
    ]
    for verdict in ("keep", "review", "junk"):
        parts.append(f"<button class=card data-key=v data-value={verdict}><b class={verdict}>{counts[verdict]}</b>"
                     f"{TITLES[verdict]}</button>")
    parts.append(f"<button class=card data-key=b data-value=-model><b>{sources['memory'] + sources['rule'] + sources['teach']}</b>"
                 "decided without the model</button></div>")
    parts.append("<h2>Overview <span class=mute>(click a bar or legend entry to filter; click again to clear)</span></h2>"
                 "<div class=overview>"
                 "<div class=viz><h3>Decision · <b id=verdict-head></b></h3><div class=bar id=verdict-bar></div>"
                 "<div class=legend id=verdict-legend></div></div>"
                 "<div class=viz><h3>Decided by · <b id=source-head></b></h3><div class=bar id=source-bar></div>"
                 "<div class=legend id=source-legend></div></div></div>")
    if plan:
        parts.append("<h2>Folder plan <span class=mute>(<code>./triage apply</code> moves mail that is still in "
                     "the scanned mailbox)</span></h2><table class=plan>")
        parts += [f"<tr><td>{e(folder)}</td><td>{n}</td></tr>" for folder, n in sorted(plan.items())]
        parts.append("</table>")
    parts.append(
        "<h2>Emails</h2><div class=tools>"
        "<input id=q type=search placeholder='Search sender, domain, subject, category… (press /)' "
        "aria-label='Search emails' autocomplete=off>"
        "<select id=f-v aria-label=Decision><option value=''>All decisions</option></select>"
        "<select id=f-c aria-label=Category><option value=''>All categories</option></select>"
        "<select id=f-l aria-label=Likelihood><option value=''>Any likelihood</option></select>"
        "<select id=f-b aria-label='Decided by'><option value=''>Decided by anyone</option></select>"
        "<button id=clear hidden>Clear filters</button><span id=count class='count mute'></span></div>"
        "<table><thead><tr>")
    for key, title in (("d", "Received"), ("f", "From"), ("s", "Subject"), ("v", "Decision"), ("c", "Category"),
                       ("l", "Likelihood"), ("b", "Decided by")):
        parts.append(f"<th data-key={key}><button type=button>{title}<span class=arrow>↕</span></button></th>")
    parts.append("<th>Why</th></tr></thead><tbody id=rows></tbody></table>"
                 "<button id=more class=more hidden></button>")
    # Without JavaScript the full table is still readable.
    parts.append("<noscript><p class=mute>Interactive filters need JavaScript; showing all emails.</p>"
                 "<table><tr><th>Received</th><th>From</th><th>Subject</th><th>Decision</th><th>Category</th>"
                 "<th>Likelihood</th><th>Decided by</th></tr>")
    for r in rows:
        parts.append(f"<tr><td>{e(r['received'][:10])}</td><td>{e(r['sender'])}</td><td>{e(r['subject'])}</td>"
                     f"<td class={e(r['verdict'])}>{e(r['verdict'])}</td><td>{e(r['category'])}</td>"
                     f"<td>{e(r['likelihood'])}</td><td>{e(r['source'])}</td></tr>")
    parts.append("</table></noscript>")
    parts.append(f"<script type=application/json id=mt-data>{_json_for_html(data)}</script>")
    parts.append(f"<script>{_JS}</script></body></html>")
    html_path = private_file(folder / f"report-{stamp}.html")
    html_path.write_text("".join(parts), encoding="utf-8")
    return html_path, csv_path
