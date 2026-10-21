/* Mail Triage add-in: read messages in Outlook and send them to Mail Triage on https://localhost only.
 * Read operations only (getAllInternetHeadersAsync, body.getAsync, loadItemByIdAsync/unloadAsync);
 * no categories, moves, flags or deletes. Results are shown in this pane only. */
"use strict";

const SETS = ["1.8", "1.13", "1.15"];
const MAX_SELECTION = 100;
const session = { done: new Set(), totals: { keep: 0, review: 0, junk: 0, errors: 0 }, cancel: false, busy: false,
                  headers: "unknown" };  // unknown | ok | unavailable (batch reads on this client)
let ready = false;

const $ = (id) => document.getElementById(id);

const STEP_TIMEOUT_MS = 30000;
const BUILD = "7";
const HEADERS_TIMEOUT_MS = 8000;

// Step diagnostics: step name, Office error code and duration only. Never content or ids.
function diag(entry) {
  fetch("/diag", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mail-Triage": "probe" },
    body: JSON.stringify(entry),
  }).catch(() => undefined);
}

async function timed(timings, step, fn) {
  const start = performance.now();
  try {
    return await fn();
  } finally {
    timings[step] = Math.round(performance.now() - start);
  }
}

function call(fn, step = "Office.js call", timeoutMs = STEP_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject({ name: `${step} timed out` }), timeoutMs);
    try {
      fn((result) => {
        clearTimeout(timer);
        if (result.status === Office.AsyncResultStatus.Succeeded) resolve(result.value);
        else reject({ name: `${step}: ${errorName(result.error)}` });
      });
    } catch (error) {
      clearTimeout(timer);
      reject({ name: `${step}: ${errorName(error)}` });
    }
  });
}

function errorName(error) {
  return (error && (error.name || error.code)) || "unknown";
}

const TOO_MANY = "More than 100 messages are selected (Outlook's limit for add-ins, and \"select all\" " +
  "always exceeds it). Click the first message, Shift-click up to 100, then choose Triage selection.";

function tooMany(error) {
  return String(errorName(error)).includes("ExceededMaxNumberOfSelectedItems");
}

function report(stage, error) {
  $("status").textContent = `${stage}: ${errorName(error)}`;
}

function supports(set) {
  return Office.context.requirements.isSetSupported("Mailbox", set);
}

function clientInfo() {
  const sets = {};
  for (const set of SETS) sets[set] = supports(set);
  const diagnostics = Office.context.diagnostics || {};
  return { platform: diagnostics.platform || "?", version: diagnostics.version || "?", sets };
}

async function readHeaders(item, timings, batch) {
  if (batch && session.headers === "unavailable") return "";
  try {
    const headers = await timed(timings, "headers", () => call((cb) => item.getAllInternetHeadersAsync(cb),
      "getAllInternetHeadersAsync", batch ? HEADERS_TIMEOUT_MS : STEP_TIMEOUT_MS));
    if (batch) session.headers = "ok";
    return headers;
  } catch (error) {
    if (!batch) throw error;
    // Outlook on the web may never answer header requests for loaded (multi-selected) items.
    // Continue without transport headers; Mail Triage then caps junk at review for these messages.
    session.headers = "unavailable";
    diag({ error: `headers unavailable in batch: ${String(errorName(error)).slice(0, 80)}`, timing: timings });
    $("headers-note").hidden = false;
    return "";
  }
}

async function readMessage(item, timings = {}, batch = false) {
  const headers = await readHeaders(item, timings, batch);
  const body = await timed(timings, "body", () =>
    call((cb) => item.body.getAsync(Office.CoercionType.Text, cb), "body.getAsync"));
  return {
    headers: headers || "",
    body: body || "",
    html: false,
    sender: (item.from && item.from.emailAddress) || "",
    subject: item.subject || "",
    internetMessageId: item.internetMessageId || "",
    received: item.dateTimeCreated ? new Date(item.dateTimeCreated).toISOString() : "",
    account: mailboxAccount(),
    client: clientInfo(),
    timing: timings,
  };
}

function mailboxAccount() {
  const profile = Office.context.mailbox && Office.context.mailbox.userProfile;
  return (profile && profile.emailAddress) || "";
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mail-Triage": "probe" },
    body: JSON.stringify(body),
  });
  return response.ok ? response.json() : null;
}

// Which Internet Message-IDs were already decided (in any earlier session)?
async function knownIds(ids) {
  if (!ids.length) return [];
  const result = await post("/known", { account: mailboxAccount(), ids }).catch(() => null);
  return (result && Array.isArray(result.known)) ? result.known : ids.map(() => false);
}

async function send(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), STEP_TIMEOUT_MS);
  const response = await fetch("/triage", {
    signal: controller.signal,
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Mail-Triage": "probe" },
    body: JSON.stringify(payload),
  }).catch((error) => { throw { name: `localhost request: ${errorName(error)}` }; })
    .finally(() => clearTimeout(timer));
  return { status: response.status, facts: await response.json() };
}

function show(facts) {
  const list = $("facts");
  list.replaceChildren();
  for (const [key, value] of Object.entries(facts)) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = String(value);
    list.append(dt, dd);
  }
}

// --- open message ----------------------------------------------------------------------

async function runSingle() {
  const item = Office.context.mailbox.item;
  if (!item) {
    $("status").textContent = "Open a message first.";
    return;
  }
  $("status").textContent = "Reading…";
  try {
    const { status, facts } = await send(await readMessage(item));
    show(Object.assign({ http_status: status }, facts, clientInfo().sets));
    $("status").textContent = status === 200 ? "Done. Read locally; nothing stored." : "Mail Triage could not parse it.";
  } catch (error) {
    report("Failed", error);
  }
}

// --- auto-triage while browsing (works where batch reading is unavailable, e.g. Outlook for Mac) ----

let autoQueued = false;

async function runAuto() {
  const item = Office.context.mailbox.item;
  if (!$("auto").checked || !item || session.busy) {
    autoQueued = Boolean(item && $("auto").checked && session.busy);
    return;
  }
  const key = item.internetMessageId || item.itemId;
  if (!key || session.done.has(key)) return;
  if (item.internetMessageId && (await knownIds([item.internetMessageId]))[0]) {
    session.done.add(key);
    return;
  }
  session.busy = true;
  try {
    const timings = {};
    const payload = await readMessage(item, timings);
    const { status, facts } = await send(payload);
    if (status !== 200) throw { name: `HTTP ${status}` };
    session.done.add(key);
    if (facts.verdict in session.totals) session.totals[facts.verdict] += 1;
    addResult(item.subject, facts);
  } catch (error) {
    session.totals.errors += 1;
    addResult(item.subject, { parsed: false, category: errorName(error) });
  } finally {
    session.busy = false;
    updateTotals();
    if (autoQueued) {
      autoQueued = false;
      runAuto();
    }
  }
}

function enableAuto() {
  const mailbox = Office.context.mailbox;
  if (!supports("1.5") || typeof mailbox.addHandlerAsync !== "function") return;
  $("auto").disabled = false;
  let registered = false;
  $("auto").addEventListener("change", () => {
    $("auto-note").hidden = !$("auto").checked;
    $("batch").hidden = false;
    if ($("auto").checked && !registered) {
      mailbox.addHandlerAsync(Office.EventType.ItemChanged, () => runAuto());
      registered = true;
    }
    if ($("auto").checked) runAuto();
  });
}

// --- batch -----------------------------------------------------------------------------

function updateTotals() {
  const t = session.totals;
  $("totals").textContent = `Session: ${session.done.size} triaged — ${t.keep} keep · ${t.review} review · ` +
    `${t.junk} junk${t.errors ? ` · ${t.errors} errors` : ""}`;
}

function addResult(subject, facts) {
  const row = document.createElement("tr");
  const verdict = facts.verdict || (facts.parsed ? "read" : "error");
  const cells = [verdict, facts.category || "", facts.headers_available === false ? "no headers" : "",
                 (subject || "").slice(0, 70)];
  cells.forEach((text, index) => {
    const cell = document.createElement("td");
    cell.textContent = text;
    if (index === 0) cell.className = `v-${verdict}`;
    row.append(cell);
  });
  $("results").prepend(row);
}

async function triageOne(selected) {
  const timings = {};
  try {
    const loaded = await timed(timings, "load", () =>
      call((cb) => Office.context.mailbox.loadItemByIdAsync(selected.itemId, cb), "loadItemByIdAsync"));
    try {
      const payload = await readMessage(loaded, timings, true);
      return await timed(timings, "localhost", () => send(payload));
    } finally {
      await timed(timings, "unload", () =>
        call((cb) => loaded.unloadAsync(cb), "unloadAsync").catch(() => undefined));
    }
  } catch (error) {
    diag({ error: String(errorName(error)).slice(0, 120), timing: timings });
    throw error;
  }
}

async function runBatch() {
  if (session.busy) return;
  if (!supports("1.15")) {
    $("batch-status").textContent = "This Outlook can see the selection but cannot read selected messages " +
      "(needs Mailbox 1.15). Use Outlook on the web or Outlook for Windows for batch triage.";
    return;
  }
  session.busy = true;
  session.cancel = false;
  $("triage").disabled = true;
  $("cancel").hidden = false;
  try {
    const selected = await call((cb) => Office.context.mailbox.getSelectedItemsAsync(cb), "getSelectedItemsAsync");
    // Clients report itemType inconsistently (e.g. "message"/"Message"); only skip explicit appointments.
    const isMessage = (item) => String(item.itemType || "message").toLowerCase() !== "appointment";
    let todo = selected.filter(isMessage).filter((item) => item.itemId && !session.done.has(item.itemId))
      .slice(0, MAX_SELECTION);
    const known = await knownIds(todo.map((item) => item.internetMessageId || ""));
    todo.forEach((item, index) => { if (known[index]) session.done.add(item.itemId); });
    todo = todo.filter((item, index) => !known[index]);
    const skipped = selected.length - todo.length;
    $("progress").hidden = false;
    $("progress").max = Math.max(todo.length, 1);
    $("progress").value = 0;
    for (const [index, item] of todo.entries()) {
      if (session.cancel) break;
      $("batch-status").textContent = `Triaging ${index + 1} of ${todo.length}${skipped ? ` (${skipped} already done)` : ""}…`;
      try {
        const { status, facts } = await triageOne(item);
        if (status !== 200) throw { name: `HTTP ${status}` };
        session.done.add(item.itemId);
        if (facts.verdict in session.totals) session.totals[facts.verdict] += 1;
        addResult(item.subject, facts);
      } catch (error) {
        session.totals.errors += 1;
        addResult(item.subject, { parsed: false, category: errorName(error) });
      }
      $("progress").value = index + 1;
      updateTotals();
    }
    $("batch-status").textContent = session.cancel ? "Cancelled." :
      `Selection done${skipped ? ` (${skipped} already triaged earlier)` : ""}. Select the next messages and repeat.`;
  } catch (error) {
    $("batch-status").textContent = tooMany(error) ? TOO_MANY : `Batch failed: ${errorName(error)}`;
  } finally {
    session.busy = false;
    $("triage").disabled = !supports("1.15");
    $("cancel").hidden = true;
  }
}

async function refreshSelection() {
  try {
    const selected = await call((cb) => Office.context.mailbox.getSelectedItemsAsync(cb), "getSelectedItemsAsync");
    const fresh = selected.filter((item) => !session.done.has(item.itemId)).length;
    $("selection").textContent = `${selected.length} selected, ${fresh} new`;
  } catch (error) {
    $("selection").textContent = tooMany(error) ? "more than 100 selected (too many)" : "";
  }
}

// --- start-up --------------------------------------------------------------------------

window.addEventListener("error", (event) => report("Script error", event.error || { name: "error" }));

setTimeout(() => {
  if (!ready) report("Office.js did not initialise within 10 s",
    { name: typeof Office === "undefined" ? "Office undefined" : "onReady not called" });
}, 10000);

if (typeof Office === "undefined") {
  report("Office.js failed to load", { name: "Office undefined" });
} else {
  Office.onReady((info) => {
    ready = true;
    $("run").disabled = false;
    $("run").addEventListener("click", runSingle);
    $("report").addEventListener("click", () => post("/report", {}).catch(() => undefined));
    enableAuto();
    $("status").textContent = "Ready.";
    const client = clientInfo();
    const names = { Mac: "Outlook for Mac", OfficeOnline: "Outlook on the web", PC: "Outlook for Windows" };
    $("client").textContent = `Build ${BUILD} · Client: ${names[client.platform] || client.platform} ${client.version} · ` +
      `batch reading (Mailbox 1.15): ${client.sets["1.15"] ? "yes" : "no"}`;
    const mailbox = Office.context.mailbox;
    if (supports("1.13") && mailbox && typeof mailbox.getSelectedItemsAsync === "function") {
      $("batch").hidden = false;
      $("triage").disabled = !supports("1.15");
      if (!supports("1.15")) $("triage").textContent = "Batch needs Outlook on the web";
      $("triage").addEventListener("click", runBatch);
      $("cancel").addEventListener("click", () => { session.cancel = true; });
      mailbox.addHandlerAsync(Office.EventType.SelectedItemsChanged, refreshSelection);
      refreshSelection();
      updateTotals();
      if (!supports("1.15")) {
        $("batch-status").textContent = "Batch reading needs Outlook on the web or Outlook for Windows " +
          "(this client lacks Mailbox 1.15). The open-message probe above works here.";
      }
    }
  });
}
