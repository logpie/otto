const state = {
  selectedRunId: null,
  detail: null,
  logOffset: 0,
  logTimer: null,
  refreshTimer: null,
  showingArtifacts: false,
  selectedArtifactIndex: null,
};

const els = {
  projectName: document.querySelector("#projectName"),
  projectBranch: document.querySelector("#projectBranch"),
  projectDirty: document.querySelector("#projectDirty"),
  watcherState: document.querySelector("#watcherState"),
  activeCount: document.querySelector("#activeCount"),
  queueCounts: document.querySelector("#queueCounts"),
  liveCount: document.querySelector("#liveCount"),
  historyCount: document.querySelector("#historyCount"),
  liveRows: document.querySelector("#liveRows"),
  historyRows: document.querySelector("#historyRows"),
  detailBody: document.querySelector("#detailBody"),
  detailStatus: document.querySelector("#detailStatus"),
  actionBar: document.querySelector("#actionBar"),
  logPane: document.querySelector("#logPane"),
  artifactPane: document.querySelector("#artifactPane"),
  refreshStatus: document.querySelector("#refreshStatus"),
  typeFilter: document.querySelector("#typeFilter"),
  outcomeFilter: document.querySelector("#outcomeFilter"),
  queryInput: document.querySelector("#queryInput"),
  activeOnly: document.querySelector("#activeOnly"),
  refreshButton: document.querySelector("#refreshButton"),
  mergeAllButton: document.querySelector("#mergeAllButton"),
  newJobButton: document.querySelector("#newJobButton"),
  startWatcherButton: document.querySelector("#startWatcherButton"),
  stopWatcherButton: document.querySelector("#stopWatcherButton"),
  jobDialog: document.querySelector("#jobDialog"),
  closeJobDialog: document.querySelector("#closeJobDialog"),
  jobForm: document.querySelector("#jobForm"),
  jobCommand: document.querySelector("#jobCommand"),
  improveModeField: document.querySelector("#improveModeField"),
  improveSubcommand: document.querySelector("#improveSubcommand"),
  jobIntent: document.querySelector("#jobIntent"),
  jobTaskId: document.querySelector("#jobTaskId"),
  jobProvider: document.querySelector("#jobProvider"),
  jobModel: document.querySelector("#jobModel"),
  jobEffort: document.querySelector("#jobEffort"),
  jobFast: document.querySelector("#jobFast"),
  jobStatus: document.querySelector("#jobStatus"),
  logsTab: document.querySelector("#logsTab"),
  artifactsTab: document.querySelector("#artifactsTab"),
  toast: document.querySelector("#toast"),
};

function params() {
  const query = new URLSearchParams();
  query.set("type", els.typeFilter.value);
  query.set("outcome", els.outcomeFilter.value);
  query.set("query", els.queryInput.value);
  query.set("active_only", els.activeOnly.checked ? "true" : "false");
  return query;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || `HTTP ${response.status}`);
  }
  return data;
}

async function refresh() {
  els.refreshStatus.textContent = "refreshing";
  try {
    const data = await api(`/api/state?${params().toString()}`);
    renderProject(data.project);
    renderWatcher(data.watcher);
    renderLive(data.live.items);
    renderHistory(data.history.items, data.history.total_rows);
    scheduleRefresh(data.live.refresh_interval_s);
    const visibleIds = new Set([
      ...data.live.items.map((item) => item.run_id),
      ...data.history.items.map((item) => item.run_id),
    ]);
    if ((!state.selectedRunId || !visibleIds.has(state.selectedRunId)) && data.live.items.length) {
      await selectRun(data.live.items[0].run_id);
    } else if ((!state.selectedRunId || !visibleIds.has(state.selectedRunId)) && data.history.items.length) {
      await selectRun(data.history.items[0].run_id);
    } else if (!visibleIds.size) {
      clearSelection();
    } else if (state.selectedRunId) {
      await loadDetail(state.selectedRunId, {keepLog: true});
    }
    els.refreshStatus.textContent = "idle";
  } catch (error) {
    els.refreshStatus.textContent = "error";
    toast(error.message, "error");
  }
}

function clearSelection() {
  state.selectedRunId = null;
  state.detail = null;
  state.logOffset = 0;
  els.detailStatus.textContent = "-";
  els.detailBody.classList.add("empty");
  els.detailBody.textContent = "Select a run.";
  els.actionBar.innerHTML = "";
  els.logPane.textContent = "";
  els.artifactPane.textContent = "";
}

function renderProject(project) {
  els.projectName.textContent = project.name || "-";
  els.projectBranch.textContent = project.branch || "-";
  els.projectDirty.textContent = project.dirty ? "dirty" : "clean";
}

function renderWatcher(watcher) {
  const counts = watcher?.counts || {};
  const active = Number(counts.running || 0) + Number(counts.starting || 0) + Number(counts.terminating || 0);
  els.watcherState.textContent = watcher?.alive ? `running pid ${watcher.watcher?.pid || "-"}` : "stopped";
  els.queueCounts.textContent = `queued ${counts.queued || 0} / active ${active} / done ${counts.done || 0}`;
  els.startWatcherButton.disabled = Boolean(watcher?.alive);
  els.stopWatcherButton.disabled = !watcher?.alive;
}

function renderLive(items) {
  els.activeCount.textContent = String(items.filter((item) => !isTerminal(item.status)).length);
  els.liveCount.textContent = String(items.length);
  els.liveRows.innerHTML = items.map((item) => `
    <tr data-run-id="${escapeAttr(item.run_id)}" class="${item.run_id === state.selectedRunId ? "selected" : ""}">
      <td class="status-${escapeAttr(item.status)}">${escapeHtml(item.status.toUpperCase())}</td>
      <td title="${escapeAttr(item.run_id)}">${escapeHtml(item.display_id || item.run_id)}</td>
      <td title="${escapeAttr(item.branch_task || "")}">${escapeHtml(item.branch_task || "-")}</td>
      <td>${escapeHtml(item.elapsed_display || "-")}</td>
      <td>${escapeHtml(item.cost_display || "-")}</td>
      <td title="${escapeAttr(item.last_event || "")}">${escapeHtml(item.last_event || "-")}</td>
    </tr>
  `).join("");
  els.liveRows.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => selectRun(row.dataset.runId));
  });
}

function renderHistory(items, totalRows) {
  els.historyCount.textContent = String(totalRows);
  els.historyRows.innerHTML = items.map((item) => `
    <tr data-run-id="${escapeAttr(item.run_id)}" class="${item.run_id === state.selectedRunId ? "selected" : ""}">
      <td class="status-${escapeAttr((item.terminal_outcome || item.status || "").toLowerCase())}">${escapeHtml(item.outcome_display || "-")}</td>
      <td title="${escapeAttr(item.run_id)}">${escapeHtml(item.queue_task_id || item.run_id)}</td>
      <td title="${escapeAttr(item.summary || "")}">${escapeHtml(item.summary || "-")}</td>
      <td>${escapeHtml(item.duration_display || "-")}</td>
      <td>${escapeHtml(item.cost_display || "-")}</td>
    </tr>
  `).join("");
  els.historyRows.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", () => selectRun(row.dataset.runId));
  });
}

async function selectRun(runId) {
  state.selectedRunId = runId;
  state.logOffset = 0;
  state.selectedArtifactIndex = null;
  els.logPane.textContent = "";
  await loadDetail(runId);
  await loadLogs({reset: true});
  document.querySelectorAll("tbody tr").forEach((row) => {
    row.classList.toggle("selected", row.dataset.runId === runId);
  });
}

async function loadDetail(runId, {keepLog = false} = {}) {
  try {
    const detail = await api(`/api/runs/${encodeURIComponent(runId)}?${params().toString()}`);
    state.detail = detail;
    els.detailStatus.textContent = detail.status || "-";
    els.detailBody.classList.remove("empty");
    els.detailBody.innerHTML = `
      <h3>${escapeHtml(detail.title || detail.run_id)}</h3>
      <dl>
        <dt>Run</dt><dd>${escapeHtml(detail.run_id)}</dd>
        <dt>Type</dt><dd>${escapeHtml(detail.domain)} / ${escapeHtml(detail.run_type)}</dd>
        <dt>Branch</dt><dd>${escapeHtml(detail.branch || "-")}</dd>
        <dt>Worktree</dt><dd>${escapeHtml(detail.worktree || detail.cwd || "-")}</dd>
        <dt>Provider</dt><dd>${escapeHtml(providerLine(detail))}</dd>
        ${detail.summary_lines.map((line) => `<dt>Info</dt><dd>${escapeHtml(line)}</dd>`).join("")}
      </dl>
    `;
    renderActions(detail.legal_actions || []);
    renderArtifacts(detail.artifacts || []);
    if (!keepLog) {
      state.logOffset = 0;
      els.logPane.textContent = "";
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderActions(actions) {
  const visible = actions.filter((action) => !["o", "e"].includes(action.key));
  els.actionBar.innerHTML = visible.map((action) => `
    <button type="button" data-action="${escapeAttr(actionName(action.key))}" ${action.enabled ? "" : "disabled"} title="${escapeAttr(action.reason || action.preview || "")}">
      ${escapeHtml(action.label)}
    </button>
  `).join("");
  els.actionBar.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => runAction(button.dataset.action));
  });
}

function renderArtifacts(artifacts) {
  if (state.selectedArtifactIndex !== null) {
    const selected = artifacts.find((artifact) => artifact.index === state.selectedArtifactIndex);
    if (selected) {
      loadArtifact(state.selectedArtifactIndex);
      return;
    }
    state.selectedArtifactIndex = null;
  }
  if (!artifacts.length) {
    els.artifactPane.textContent = "No artifacts.";
    return;
  }
  els.artifactPane.innerHTML = artifacts.map((artifact) => `
    <button type="button" data-artifact-index="${artifact.index}">
      ${escapeHtml(artifact.label)} ${artifact.exists ? "" : "(missing)"}
    </button>
  `).join("\n");
  els.artifactPane.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => loadArtifact(Number(button.dataset.artifactIndex)));
  });
}

async function loadLogs({reset = false} = {}) {
  if (!state.selectedRunId || state.showingArtifacts) return;
  try {
    const offset = reset ? 0 : state.logOffset;
    const data = await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/logs?offset=${offset}`);
    if (reset) els.logPane.textContent = "";
    if (data.text) {
      els.logPane.textContent += data.text;
      els.logPane.scrollTop = els.logPane.scrollHeight;
    }
    state.logOffset = data.next_offset || offset;
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadArtifact(index) {
  if (!state.selectedRunId) return;
  state.selectedArtifactIndex = index;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/artifacts/${index}/content`);
    els.artifactPane.innerHTML = `
      <button type="button" data-artifact-back>Back to artifacts</button>
      <pre>${escapeHtml(data.content || "")}</pre>
    `;
    const backButton = els.artifactPane.querySelector("[data-artifact-back]");
    if (backButton) {
      backButton.addEventListener("click", () => {
        state.selectedArtifactIndex = null;
        renderArtifacts(state.detail?.artifacts || []);
      });
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

async function runAction(action) {
  if (!state.selectedRunId) return;
  if (!confirm(`Run ${action}?`)) return;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(state.selectedRunId)}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    toast(data.message || `${action} requested`);
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function queueJob(event) {
  event.preventDefault();
  const command = els.jobCommand.value;
  const intent = els.jobIntent.value.trim();
  const taskId = els.jobTaskId.value.trim();
  const provider = els.jobProvider.value.trim();
  const model = els.jobModel.value.trim();
  const effort = els.jobEffort.value.trim();
  const payload = {};
  if (taskId) payload.as = taskId;
  payload.extra_args = [];
  if (provider) payload.extra_args.push("--provider", provider);
  if (model) payload.extra_args.push("--model", model);
  if (effort) payload.extra_args.push("--effort", effort);
  if (els.jobFast.checked) payload.extra_args.push("--fast");
  if (command === "build") {
    payload.intent = intent;
  } else if (command === "improve") {
    payload.subcommand = els.improveSubcommand.value;
    if (intent) payload.focus = intent;
  } else if (intent) {
    payload.intent = intent;
  }
  els.jobStatus.textContent = "queueing";
  try {
    const data = await api(`/api/queue/${command}`, {method: "POST", body: JSON.stringify(payload)});
    els.jobStatus.textContent = "";
    els.jobDialog.close();
    els.jobForm.reset();
    toast(data.message || "queued");
    await refresh();
  } catch (error) {
    els.jobStatus.textContent = error.message;
  }
}

function scheduleRefresh(seconds) {
  clearTimeout(state.refreshTimer);
  const delay = Math.max(500, Math.min(5000, Number(seconds || 1.5) * 1000));
  state.refreshTimer = setTimeout(refresh, delay);
}

function providerLine(detail) {
  return [detail.provider, detail.model, detail.reasoning_effort].filter(Boolean).join(" / ") || "-";
}

function actionName(key) {
  return {c: "cancel", r: "resume", R: "retry", x: "cleanup", m: "merge", M: "merge-all"}[key] || key;
}

function isTerminal(status) {
  return ["done", "failed", "cancelled", "removed", "interrupted"].includes(status);
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("visible");
  setTimeout(() => els.toast.classList.remove("visible"), 2800);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[ch]));
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#096;");
}

els.refreshButton.addEventListener("click", refresh);
els.mergeAllButton.addEventListener("click", async () => {
  if (!confirm("Merge all done queue tasks?")) return;
  try {
    const data = await api("/api/actions/merge-all", {method: "POST", body: "{}"});
    toast(data.message || "merge all requested");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  }
});
els.newJobButton.addEventListener("click", () => els.jobDialog.showModal());
els.startWatcherButton.addEventListener("click", async () => {
  try {
    const data = await api("/api/watcher/start", {method: "POST", body: JSON.stringify({concurrent: 2})});
    toast(data.message || "watcher started");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  }
});
els.stopWatcherButton.addEventListener("click", async () => {
  if (!confirm("Stop the queue watcher? Running tasks will be interrupted.")) return;
  try {
    const data = await api("/api/watcher/stop", {method: "POST", body: "{}"});
    toast(data.message || "watcher stop requested");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
  }
});
els.closeJobDialog.addEventListener("click", () => els.jobDialog.close());
els.jobForm.addEventListener("submit", queueJob);
els.jobCommand.addEventListener("change", () => {
  els.improveModeField.classList.toggle("hidden", els.jobCommand.value !== "improve");
});
els.logsTab.addEventListener("click", () => {
  state.showingArtifacts = false;
  state.selectedArtifactIndex = null;
  els.logsTab.classList.add("active");
  els.artifactsTab.classList.remove("active");
  els.logPane.classList.remove("hidden");
  els.artifactPane.classList.add("hidden");
  loadLogs({reset: true});
});
els.artifactsTab.addEventListener("click", () => {
  state.showingArtifacts = true;
  els.artifactsTab.classList.add("active");
  els.logsTab.classList.remove("active");
  els.artifactPane.classList.remove("hidden");
  els.logPane.classList.add("hidden");
});
[els.typeFilter, els.outcomeFilter, els.activeOnly].forEach((el) => el.addEventListener("change", refresh));
els.queryInput.addEventListener("input", () => {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(refresh, 250);
});
state.logTimer = setInterval(() => loadLogs(), 1200);
refresh();
