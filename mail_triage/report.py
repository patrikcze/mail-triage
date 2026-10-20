"""Reports: a CSV and a single self-contained HTML page. Both stay in the private data folder.

The HTML page loads nothing from the internet (its Content-Security-Policy forbids it), so
opening it in a browser cannot leak anything.
"""

from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime
import html
import json
from pathlib import Path

from .config import Config
from .privacy import csv_safe, private_dir, private_file
from .store import Store


ORDER = {"junk": 0, "review": 1, "keep": 2}
TITLES = {"junk": "Junk (highly likely)", "review": "To review", "keep": "For you"}
_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--junk:#c0392b;--review:#b7791f;--keep:#2f855a}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f2f2f7;--mute:#a1a1a6;--line:#3a3a3c}}
body{background:var(--bg);color:var(--fg);font:14px -apple-system,Segoe UI,sans-serif;margin:24px auto;max-width:1200px;padding:0 16px}
h1{font-size:22px}h2{font-size:17px;margin-top:28px}.mute{color:var(--mute)}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card{border:1px solid var(--line);border-radius:10px;padding:10px 16px}
.card b{font-size:22px;display:block}table{border-collapse:collapse;width:100%}
td,th{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
th{font-weight:600;color:var(--mute)}.junk{color:var(--junk)}.review{color:var(--review)}.keep{color:var(--keep)}
details summary{cursor:pointer}td.r{font-size:12px;color:var(--mute)}
"""


def _rows(store: Store, accounts: list[str] | None):
    rows = store.rows(accounts)
    return sorted(rows, key=lambda r: (ORDER.get(r["verdict"], 3), -r["junk_probability"]))


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
    e = html.escape
    parts = [
        "<!doctype html><html lang=en><head><meta charset=utf-8>",
        "<meta http-equiv=Content-Security-Policy content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        "<meta name=viewport content='width=device-width,initial-scale=1'>",
        f"<title>Mail Triage report</title><style>{_CSS}</style></head><body>",
        f"<h1>Mail Triage report</h1><p class=mute>{e(datetime.now().strftime('%Y-%m-%d %H:%M'))} · "
        f"local file, nothing was sent anywhere · settings {e(config.fingerprint())}</p><div class=cards>",
    ]
    for verdict in ("keep", "review", "junk"):
        parts.append(f"<div class=card><b class={verdict}>{counts[verdict]}</b>{TITLES[verdict]}</div>")
    parts.append(f"<div class=card><b>{sources['memory'] + sources['rule'] + sources['teach']}</b>"
                 "decided without the model</div></div>")
    if plan:
        parts.append("<h2>Folder plan <span class=mute>(preview — nothing is moved)</span></h2><table>")
        parts += [f"<tr><td>{e(folder)}</td><td>{n}</td></tr>" for folder, n in sorted(plan.items())]
        parts.append("</table>")
    for verdict in ("junk", "review", "keep"):
        group = [r for r in rows if r["verdict"] == verdict]
        if not group:
            continue
        parts.append(f"<h2 class={verdict}>{TITLES[verdict]} · {len(group)}</h2><table><tr><th>Received</th>"
                     "<th>From</th><th>Subject</th><th>Category</th><th>Likelihood</th><th>Decided by</th>"
                     "<th>Why</th></tr>")
        for r in group:
            reasons = "<br>".join(e(x) for x in json.loads(r["reasons"]))
            parts.append(f"<tr><td>{e(r['received'][:10])}</td><td>{e(r['sender'])}</td>"
                         f"<td>{e(r['subject']) or '<span class=mute>(not stored)</span>'}</td>"
                         f"<td>{e(r['category'])}</td><td>{e(r['likelihood'])}</td><td>{e(r['source'])}</td>"
                         f"<td class=r><details><summary>show</summary>{reasons}</details></td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    html_path = private_file(folder / f"report-{stamp}.html")
    html_path.write_text("".join(parts), encoding="utf-8")
    return html_path, csv_path
