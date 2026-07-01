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
const PROGRESS_PATH = "docs/data/progress.json";
const TOKEN_KEY = "ssq_gh_token";

const API_BASE = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}`;

let latestData = { results: [] };

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

function renderTable() {
  const threshold = Number(el("threshold").value) / 100;
  const body = el("results-body");
  body.innerHTML = "";

  const visible = (latestData.results || []).filter(
    (r) => (r.expansion_progress ?? 0) >= threshold
  );

  el("total-count").textContent = (latestData.results || []).length;
  el("visible-count").textContent = visible.length;

  el("empty-state").style.display = visible.length ? "none" : "block";
  el("results-table").style.display = visible.length ? "table" : "none";

  for (const r of visible) {
    const tr = document.createElement("tr");
    const pct = Math.min(100, Math.round((r.expansion_progress ?? 0) * 100));
    const done = r.status === "match";
    tr.innerHTML = `
      <td class="ticker">${r.ticker}</td>
      <td><span class="badge ${r.status}">${r.status === "match" ? "TRIGGERED" : "WATCH"}</span></td>
      <td>${fmtMoney(r.current_price)}</td>
      <td>${fmtMoney(r.expected_low)} &ndash; ${fmtMoney(r.expected_high)}</td>
      <td>${fmtMoney(r.year_high)}</td>
      <td>${fmtMoney(r.year_low)}</td>
      <td>
        <div class="progress-cell">
          <div class="progress-bar"><div class="${done ? "done" : ""}" style="width:${pct}%"></div></div>
          <span>${pct}%</span>
        </div>
      </td>
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
  el("last-updated").textContent = latestData.generated_at
    ? `last updated ${new Date(latestData.generated_at).toLocaleString()}`
    : "no data yet";
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

function renderScanProgress(data) {
  if (!data || !data.total) return;
  el("scan-progress-panel").style.display = "block";
  el("scan-current-ticker").textContent = data.current || "–";
  el("scan-counts").textContent = `${data.scanned} / ${data.total} scanned`;

  const grid = el("scan-ticker-grid");
  grid.innerHTML = data.tickers
    .map((t, i) => {
      const s = data.status[i];
      const cls =
        t === data.current ? "scan-current" :
        s === "watch" || s === "match" ? "scan-hit" :
        s === "pending" || s === "scanning" ? "scan-pending" : "scan-none";
      return `<span class="scan-chip ${cls}">${t}</span>`;
    })
    .join("");
}

async function fetchAndRenderProgress() {
  try {
    const data = await fetchJsonViaContentsApi(PROGRESS_PATH);
    if (data) renderScanProgress(data);
  } catch (err) {
    // Best effort -- live progress is a nice-to-have, don't fail the whole refresh over it.
  }
}

async function triggerRefresh() {
  const token = getToken();
  if (!token) {
    openSettings();
    setStatus("Add a GitHub token first (see the panel that just opened).", "error");
    return;
  }

  const btn = el("btn-refresh");
  btn.disabled = true;

  try {
    setStatus("Triggering a new scan...");
    const dispatchedAt = Date.now();
    const dispatchRes = await ghApi(`/actions/workflows/${WORKFLOW_FILE}/dispatches`, {
      method: "POST",
      body: JSON.stringify({ ref: BRANCH }),
    });
    if (!dispatchRes.ok) {
      const body = await dispatchRes.text();
      if (dispatchRes.status === 403) {
        throw new Error(
          `HTTP 403 from GitHub: fine-grained tokens are unreliable for triggering workflow runs, ` +
          `even with Actions: Read and write set. Open Settings (⚙) and use a classic token ` +
          `(github.com/settings/tokens/new) with the "repo" and "workflow" scopes checked instead.`
        );
      }
      throw new Error(`Couldn't start the workflow (HTTP ${dispatchRes.status}): ${body.slice(0, 200)}`);
    }

    setStatus("Scan queued, waiting for it to start...");
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
    if (!run) throw new Error("Scan was triggered but didn't show up in the run list in time. Check the Actions tab.");

    setStatus(`Scan running (this can take a few minutes for the full ticker universe)...`);
    while (run.status !== "completed") {
      await sleep(6000);
      const runRes = await ghApi(`/actions/runs/${run.id}`);
      if (runRes.ok) run = await runRes.json();
      await fetchAndRenderProgress();
      setStatus(`Scan ${run.status}...`);
    }
    await fetchAndRenderProgress(); // catch the final state

    if (run.conclusion !== "success") {
      setStatus(`Scan finished with status "${run.conclusion}". Check the Actions tab for logs. Loading last saved results instead.`, "error");
    } else {
      setStatus("Scan complete, loading fresh results...");
    }

    await loadFreshResultsViaApi();
    setStatus(`Done. Results updated ${new Date(latestData.generated_at).toLocaleString()}.`, "ok");
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
  el("last-updated").textContent = latestData.generated_at
    ? `last updated ${new Date(latestData.generated_at).toLocaleString()}`
    : "no data yet";
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
    await loadSavedResults(true);
    setStatus("Reloaded.", "ok");
  } catch (err) {
    setStatus(err.message || String(err), "error");
  }
});

el("btn-refresh").addEventListener("click", triggerRefresh);
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
