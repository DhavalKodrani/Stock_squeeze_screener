// ---------------------------------------------------------------------------
// Stock Squeeze Screener - GitHub Pages UI
// Reads docs/data/results.json (written by screener.py) and, optionally,
// triggers a fresh GitHub Actions run via the REST API using a token the
// user provides once (stored only in localStorage, never committed).
// ---------------------------------------------------------------------------

const REPO_OWNER = "DhavalKodrani";
const REPO_NAME = "Stock_squeeze_screener";
const WORKFLOW_FILE = "daily-screener.yml";
const BRANCH = "main";
const RESULTS_PATH = "docs/data/results.json";
const CUSTOM_RESULTS_PATH = "docs/data/custom_results.json";
const PROGRESS_PATH = "docs/data/progress.json";
const TOKEN_KEY = "ssq_gh_token";

const API_BASE = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}`;

let latestData = { results: [] };
let viewMode = "full"; // "full" = daily scan results, "custom" = user-provided ticker list

const el = (id) => document.getElementById(id);

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function setStatus(msg, kind) {
  const line = el("status-line");
  line.textContent = msg;
  line.className = kind ? kind : "";
}

function fmtMoney(v) {
  return v === null || v === undefined ? "&ndash;" : `$${Number(v).toFixed(4)}`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return null;
  const s = Math.round(seconds);
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return m > 0 ? `${m}m ${rem}s` : `${rem}s`;
}

function updateLastUpdatedLabel() {
  if (!latestData.generated_at) {
    el("last-updated").textContent = "no data yet";
    return;
  }
  const when = new Date(latestData.generated_at).toLocaleString();
  const dur = fmtDuration(latestData.duration_seconds);
  el("last-updated").textContent = dur ? `last updated ${when} · took ${dur}` : `last updated ${when}`;
}

function renderTable() {
  const threshold = Number(el("threshold").value) / 100;
  const body = el("results-body");
  body.innerHTML = "";

  // Custom-list mode shows every requested ticker unconditionally -- the
  // whole point is a quick glance at YOUR list, including non-qualifiers.
  const visible = viewMode === "custom"
    ? (latestData.results || [])
    : (latestData.results || []).filter((r) => (r.expansion_progress ?? 0) >= threshold);

  el("total-count").textContent = (latestData.results || []).length;
  el("visible-count").textContent = visible.length;

  el("custom-banner").style.display = viewMode === "custom" ? "block" : "none";
  if (viewMode === "custom") el("custom-count").textContent = (latestData.results || []).length;

  el("empty-state").style.display = visible.length ? "none" : "block";
  el("results-table").style.display = visible.length ? "table" : "none";

  for (const r of visible) {
    const tr = document.createElement("tr");
    const pct = Math.min(100, Math.round((r.expansion_progress ?? 0) * 100));
    const done = r.status === "match";

    // Signals cell: "6/8" with a hover tooltip listing every indicator vote
    // (RSI, MACD, Momentum, LSMA, EMA, Ichimoku, VWAP, MFI).
    let signalsHtml = "&ndash;";
    if (r.confirmations_total) {
      const tip = Object.entries(r.indicators || {})
        .map(([k, d]) => `${d.pass ? "✓" : "✗"} ${k.toUpperCase()}${d.value !== null && d.value !== undefined ? " = " + d.value : ""} — ${d.desc}`)
        .join("\n");
      const cls = r.confirmations_passed === r.confirmations_total ? "sig-all" : "sig-some";
      signalsHtml = `<span class="signals ${cls}" title="${tip.replace(/"/g, "&quot;")}">${r.confirmations_passed}/${r.confirmations_total}</span>`;
    }

    const badgeLabel = r.status === "match" ? "TRIGGERED" : r.status === "watch" ? "WATCH" : "NO SETUP";

    tr.innerHTML = `
      <td class="ticker">${r.ticker}</td>
      <td><span class="badge ${r.status}">${badgeLabel}</span></td>
      <td>${fmtMoney(r.current_price)}</td>
      <td>${r.expected_low != null ? `${fmtMoney(r.expected_low)} &ndash; ${fmtMoney(r.expected_high)}` : "&ndash;"}</td>
      <td>${fmtMoney(r.year_high)}</td>
      <td>${fmtMoney(r.year_low)}</td>
      <td>
        <div class="progress-cell">
          <div class="progress-bar"><div class="${done ? "done" : ""}" style="width:${pct}%"></div></div>
          <span>${pct}%</span>
        </div>
      </td>
      <td>${signalsHtml}</td>
      <td class="notes">${(r.notes || []).join("<br>")}</td>
    `;
    body.appendChild(tr);
  }
}

async function loadSavedResults(bustCache) {
  const url = bustCache ? `data/results.json?_=${Date.now()}` : "data/results.json";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Couldn't load results.json (HTTP ${res.status})`);
  latestData = await res.json();
  updateLastUpdatedLabel();
  renderTable();
}

async function ghApi(path, opts = {}) {
  const token = getToken();
  const res = await fetch(`${API_BASE}${path}`, {
    ...opts,
    headers: {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(opts.headers || {}),
    },
  });
  return res;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// --- Live scan progress ----------------------------------------------------
// GitHub Actions job logs are only readable once a job *finishes* (the
// live-log blob returns 404 "BlobNotFound" while a job is still running --
// confirmed, there's no way to stream them live). So instead screener.py
// itself commits+pushes docs/data/progress.json periodically while
// scanning, and we poll that here via the Contents API, same mechanism as
// results.json.

async function fetchJsonViaContentsApi(path) {
  const res = await ghApi(`/contents/${path}?ref=${BRANCH}`);
  if (!res.ok) return null;
  const body = await res.json();
  const bytes = Uint8Array.from(atob(body.content.replace(/\n/g, "")), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder("utf-8").decode(bytes));
}

// Rebuilding all ~4000 chip DOM nodes on every poll (every few seconds) is
// what made the grid scroll janky -- each rebuild forces the browser to
// re-layout the whole scroll container mid-scroll. Instead we build the
// chip nodes once per ticker list and, on later polls, only touch the
// className of nodes whose state actually changed.
let scanGrid = { tickers: null, nodes: [] };

function ensureScanGridBuilt(tickers) {
  const same =
    scanGrid.tickers &&
    scanGrid.tickers.length === tickers.length &&
    scanGrid.tickers.every((t, i) => t === tickers[i]);
  if (same) return;

  const grid = el("scan-ticker-grid");
  const frag = document.createDocumentFragment();
  const nodes = new Array(tickers.length);
  for (let i = 0; i < tickers.length; i++) {
    const span = document.createElement("span");
    span.className = "scan-chip scan-pending";
    span.textContent = tickers[i];
    nodes[i] = span;
    frag.appendChild(span);
  }
  grid.innerHTML = "";
  grid.appendChild(frag);
  scanGrid = { tickers, nodes };
}

function renderScanProgress(data) {
  if (!data || !data.total) return;
  el("scan-progress-panel").style.display = "block";
  el("scan-current-ticker").textContent = data.current || "–";
  el("scan-counts").textContent = `${data.scanned} / ${data.total} scanned`;

  ensureScanGridBuilt(data.tickers);
  for (let i = 0; i < data.tickers.length; i++) {
    const s = data.status[i];
    const cls =
      data.tickers[i] === data.current ? "scan-current" :
      s === "watch" || s === "match" ? "scan-hit" :
      s === "pending" || s === "scanning" ? "scan-pending" : "scan-none";
    const node = scanGrid.nodes[i];
    const fullClass = "scan-chip " + cls;
    if (node.className !== fullClass) node.className = fullClass;
  }
}

async function fetchAndRenderProgress() {
  try {
    const data = await fetchJsonViaContentsApi(PROGRESS_PATH);
    if (data) renderScanProgress(data);
  } catch (err) {
    // Best effort -- live progress is a nice-to-have, don't fail the whole refresh over it.
  }
}

// Dispatches the workflow (optionally with inputs, e.g. a custom ticker
// list), waits for the run to finish while streaming progress, and returns
// the completed run object. Shared by "Refresh Now" and "Analyze My List".
async function dispatchAndWait(inputs, runningLabel) {
  const dispatchedAt = Date.now();
  const body = { ref: BRANCH };
  if (inputs && Object.keys(inputs).length) body.inputs = inputs;
  const dispatchRes = await ghApi(`/actions/workflows/${WORKFLOW_FILE}/dispatches`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  if (!dispatchRes.ok) {
    const errBody = await dispatchRes.text();
    if (dispatchRes.status === 403) {
      throw new Error(
        `HTTP 403 from GitHub: fine-grained tokens are unreliable for triggering workflow runs, ` +
        `even with Actions: Read and write set. Open Settings (⚙) and use a classic token ` +
        `(github.com/settings/tokens/new) with the "repo" and "workflow" scopes checked instead.`
      );
    }
    throw new Error(`Couldn't start the workflow (HTTP ${dispatchRes.status}): ${errBody.slice(0, 200)}`);
  }

  setStatus("Run queued, waiting for it to start...");
  let run = null;
  for (let i = 0; i < 20; i++) {
    await sleep(3000);
    const runsRes = await ghApi(
      `/actions/workflows/${WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=5`
    );
    if (!runsRes.ok) continue;
    const data = await runsRes.json();
    const candidate = (data.workflow_runs || []).find(
      (r) => new Date(r.created_at).getTime() >= dispatchedAt - 5000
    );
    if (candidate) {
      run = candidate;
      break;
    }
  }
  if (!run) throw new Error("Run was triggered but didn't show up in the run list in time. Check the Actions tab.");

  setStatus(runningLabel);
  while (run.status !== "completed") {
    await sleep(6000);
    const runRes = await ghApi(`/actions/runs/${run.id}`);
    if (runRes.ok) run = await runRes.json();
    await fetchAndRenderProgress();
    setStatus(`Run ${run.status}...`);
  }
  await fetchAndRenderProgress(); // catch the final state
  return run;
}

function requireToken() {
  const token = getToken();
  if (!token) {
    openSettings();
    setStatus("Add a GitHub token first (see the panel that just opened).", "error");
    return false;
  }
  return true;
}

async function triggerRefresh() {
  if (!requireToken()) return;

  const btn = el("btn-refresh");
  btn.disabled = true;

  try {
    setStatus("Triggering a new scan...");
    const run = await dispatchAndWait(null, "Scan running (this can take a few minutes for the full ticker universe)...");

    if (run.conclusion !== "success") {
      setStatus(`Scan finished with status "${run.conclusion}". Check the Actions tab for logs. Loading last saved results instead.`, "error");
    } else {
      setStatus("Scan complete, loading fresh results...");
    }

    viewMode = "full";
    await loadFreshResultsViaApi();
    setStatus(`Done. Results updated ${new Date(latestData.generated_at).toLocaleString()}.`, "ok");
  } catch (err) {
    setStatus(err.message || String(err), "error");
  } finally {
    btn.disabled = false;
  }
}

async function analyzeCustomList() {
  const raw = el("custom-tickers").value.trim();
  if (!raw) {
    setStatus("Enter one or more comma-separated tickers first (e.g. MBOT, PLUG, TSLA).", "error");
    return;
  }
  const tickers = [...new Set(raw.split(",").map((t) => t.trim().toUpperCase()).filter(Boolean))];
  if (!tickers.length) {
    setStatus("Couldn't parse any tickers from that input.", "error");
    return;
  }
  const bad = tickers.filter((t) => !/^[A-Z0-9.\-]{1,10}$/.test(t));
  if (bad.length) {
    setStatus(`These don't look like valid ticker symbols: ${bad.join(", ")}`, "error");
    return;
  }
  if (tickers.length > 50) {
    setStatus("Please keep the custom list to 50 tickers or fewer.", "error");
    return;
  }
  if (!requireToken()) return;

  const btn = el("btn-analyze");
  btn.disabled = true;

  try {
    setStatus(`Analyzing ${tickers.length} ticker(s): ${tickers.join(", ")}...`);
    const run = await dispatchAndWait(
      { tickers: tickers.join(",") },
      `Analyzing your ${tickers.length} ticker(s) (usually under a minute)...`
    );

    if (run.conclusion !== "success") {
      throw new Error(`Analysis run finished with status "${run.conclusion}". Check the Actions tab for logs.`);
    }

    const data = await fetchJsonViaContentsApi(CUSTOM_RESULTS_PATH);
    if (!data || !data.generated_at) {
      throw new Error("Analysis finished but the results file couldn't be loaded. Try again in a few seconds.");
    }
    latestData = data;
    viewMode = "custom";
    updateLastUpdatedLabel();
    renderTable();
    setStatus(`Custom analysis done: ${data.counts.match} triggered, ${data.counts.watch} on watch, ${data.counts.none} no setup.`, "ok");
  } catch (err) {
    setStatus(err.message || String(err), "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadFreshResultsViaApi() {
  const token = getToken();
  if (!token) return loadSavedResults(true);

  const data = await fetchJsonViaContentsApi(RESULTS_PATH);
  if (!data) {
    // Fall back to the static file (may lag a little behind Pages' CDN cache).
    return loadSavedResults(true);
  }
  latestData = data;
  updateLastUpdatedLabel();
  renderTable();
}

function openSettings() {
  el("token-input").value = getToken();
  el("settings-modal").classList.add("open");
}
function closeSettings() {
  el("settings-modal").classList.remove("open");
}

el("threshold").addEventListener("input", () => {
  el("threshold-value").textContent = el("threshold").value;
  renderTable();
});

el("btn-reload").addEventListener("click", async () => {
  setStatus("Reloading saved results...");
  try {
    viewMode = "full";
    await loadSavedResults(true);
    setStatus("Reloaded.", "ok");
  } catch (err) {
    setStatus(err.message || String(err), "error");
  }
});

el("btn-refresh").addEventListener("click", triggerRefresh);
el("btn-analyze").addEventListener("click", analyzeCustomList);
el("custom-tickers").addEventListener("keydown", (e) => {
  if (e.key === "Enter") analyzeCustomList();
});
el("btn-back-full").addEventListener("click", async () => {
  viewMode = "full";
  setStatus("Loading full scan results...");
  try {
    await loadSavedResults(true);
    setStatus("Showing full scan results.", "ok");
  } catch (err) {
    setStatus(err.message || String(err), "error");
  }
});
el("btn-settings").addEventListener("click", openSettings);
el("btn-close-modal").addEventListener("click", closeSettings);
el("btn-save-token").addEventListener("click", () => {
  const val = el("token-input").value.trim();
  if (val) localStorage.setItem(TOKEN_KEY, val);
  closeSettings();
  setStatus("Token saved.", "ok");
});
el("btn-clear-token").addEventListener("click", () => {
  localStorage.removeItem(TOKEN_KEY);
  el("token-input").value = "";
  setStatus("Token cleared.", "ok");
});

// Initial load: no token needed, just show whatever the last run produced.
loadSavedResults(false).catch((err) => setStatus(err.message || String(err), "error"));
