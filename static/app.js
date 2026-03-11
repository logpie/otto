// static/app.js — CC Autonomous v2 frontend

const API = '';
let currentFilter = 'all';
let openLogs = new Set();
let evtSource = null;
let lastTasks = [];
let lastWorkers = {};

// -- Initialization ----------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  // Restore project dir from localStorage
  const saved = localStorage.getItem('cc-auto-project-dir');
  if (saved) document.getElementById('projectDir').value = saved;

  document.getElementById('projectDir').addEventListener('input', e => {
    localStorage.setItem('cc-auto-project-dir', e.target.value);
    debounce(refreshProjectInfo, 500)();
  });

  // Enter to submit task
  document.getElementById('taskInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      addTask();
    }
  });

  // Request notification permission
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }

  // Start SSE
  connectSSE();
  refreshProjectInfo();
});


// -- SSE Connection ----------------------------------------------------------

function connectSSE() {
  if (evtSource) evtSource.close();

  evtSource = new EventSource(API + '/api/events');

  evtSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    const oldTasks = lastTasks;
    lastTasks = data.tasks || [];
    lastWorkers = data.workers || {};

    // Detect task completions/failures for notifications
    for (const task of lastTasks) {
      const old = oldTasks.find(t => t.id === task.id);
      if (old && old.status === 'in_progress' && task.status === 'completed') {
        notify('success', `Task completed: ${task.prompt.slice(0, 60)}`);
      } else if (old && old.status === 'in_progress' && task.status === 'failed') {
        notify('error', `Task failed: ${task.prompt.slice(0, 60)}`);
      }
    }

    renderStatusBar();
    renderWorkers();
    renderTasks();
  };

  evtSource.onerror = () => {
    document.getElementById('statusSummary').textContent = 'Disconnected. Reconnecting...';
  };
}


// -- Rendering ---------------------------------------------------------------

function renderStatusBar() {
  const counts = { pending: 0, in_progress: 0, completed: 0, failed: 0 };
  let totalCost = 0;
  for (const t of lastTasks) {
    counts[t.status] = (counts[t.status] || 0) + 1;
    totalCost += t.cost_usd || 0;
  }

  const parts = [];
  if (counts.pending) parts.push(`<span class="count">${counts.pending}</span> pending`);
  if (counts.in_progress) parts.push(`<span class="count">${counts.in_progress}</span> running`);
  if (counts.completed) parts.push(`<span class="count">${counts.completed}</span> done`);
  if (counts.failed) parts.push(`<span class="count">${counts.failed}</span> failed`);
  if (totalCost > 0) parts.push(`<span class="cost">$${totalCost.toFixed(2)}</span>`);

  document.getElementById('statusSummary').innerHTML = parts.join(' &middot; ') || 'No tasks';
}

function renderWorkers() {
  const el = document.getElementById('workerList');
  const entries = Object.entries(lastWorkers);
  if (entries.length === 0) {
    el.innerHTML = '<div class="empty-state">No workers running</div>';
    return;
  }

  el.innerHTML = entries.map(([name, w]) => {
    // Find the task this worker is working on
    const currentTask = lastTasks.find(
      t => t.worker === name && t.status === 'in_progress'
    );
    const taskInfo = currentTask
      ? `working on <em>${esc(currentTask.prompt.slice(0, 40))}</em>`
      : '';

    return `
      <div class="worker-card">
        <span class="badge ${w.status}">${w.status}</span>
        <span class="worker-name">${esc(name)}</span>
        <span class="worker-meta">${taskInfo}</span>
        ${w.status === 'running'
          ? `<button class="btn danger tiny" onclick="stopWorker('${esc(name)}')">Stop</button>`
          : ''}
      </div>`;
  }).join('');
}

function renderTasks() {
  const el = document.getElementById('taskList');
  let tasks = lastTasks.slice().reverse();

  // Apply filter
  if (currentFilter === 'active') {
    tasks = tasks.filter(t => t.status === 'pending' || t.status === 'in_progress');
  } else if (currentFilter === 'done') {
    tasks = tasks.filter(t => t.status === 'completed' || t.status === 'failed');
  }

  el.innerHTML = tasks.map(t => {
    const attempts = t.attempts || 0;
    const maxRetries = t.max_retries || 3;
    const cost = t.cost_usd ? `$${t.cost_usd.toFixed(3)}` : '';
    const elapsed = t.started_at ? formatElapsed(t.started_at, t.finished_at) : '';
    const verifyGoal = t.verify_prompt || t.verify || '';

    // Build attempt dots
    let attemptDots = '';
    if (t.status === 'in_progress' || t.status === 'failed' || attempts > 0) {
      const dots = [];
      for (let i = 1; i <= maxRetries; i++) {
        if (attempts === 0 && t.status === 'in_progress' && i === 1)
          dots.push('<span class="attempt-dot active"></span>');
        else if (i < attempts) dots.push('<span class="attempt-dot done"></span>');
        else if (i === attempts && t.status === 'in_progress')
          dots.push('<span class="attempt-dot active"></span>');
        else if (i === attempts && t.status === 'failed')
          dots.push('<span class="attempt-dot fail"></span>');
        else if (i <= attempts && t.status === 'completed')
          dots.push('<span class="attempt-dot done"></span>');
        else dots.push('<span class="attempt-dot"></span>');
      }
      attemptDots = `<span class="attempt-bar">${dots.join('')}</span>`;
    }

    return `
      <div class="task-card ${t.status}">
        <div class="task-prompt">${esc(t.prompt)}</div>
        ${verifyGoal ? `<div class="task-verify-goal">Verify: ${esc(verifyGoal)}</div>` : ''}
        <div class="task-meta">
          <span class="badge ${t.status}">${t.status}</span>
          ${attemptDots}
          ${cost ? `<span class="cost">${cost}</span>` : ''}
          ${elapsed ? `<span>${elapsed}</span>` : ''}
          ${t.worker ? `<span>${esc(t.worker)}</span>` : ''}
          <span style="color:var(--text-muted)">${t.id}</span>
        </div>
        <div class="task-actions">
          <button class="btn secondary tiny" onclick="toggleLog('${t.id}')">Log</button>
          ${['failed', 'completed'].includes(t.status)
            ? `<button class="btn primary tiny" onclick="retryTask('${t.id}')">Retry</button>`
            : ''}
          <button class="btn danger tiny" onclick="deleteTask('${t.id}')">Delete</button>
        </div>
        <div class="log-box" id="log-${t.id}" style="display:none"></div>
      </div>`;
  }).join('');

  // Re-open logs that were open
  for (const id of openLogs) {
    const logEl = document.getElementById('log-' + id);
    if (logEl) {
      logEl.style.display = 'block';
      refreshLog(id);
    }
  }
}


// -- Actions -----------------------------------------------------------------

async function addTask() {
  const input = document.getElementById('taskInput');
  const prompt = input.value.trim();
  if (!prompt) return;

  const verifyPrompt = document.getElementById('verifyPromptInput').value.trim();
  const verifyCmd = document.getElementById('verifyCmdInput').value.trim();
  const maxRetries = parseInt(document.getElementById('maxRetriesInput').value) || 3;

  await fetch(API + '/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      prompt,
      verify_prompt: verifyPrompt,
      verify_cmd: verifyCmd,
      max_retries: maxRetries,
    }),
  });

  input.value = '';
  document.getElementById('verifyPromptInput').value = '';
  document.getElementById('verifyCmdInput').value = '';
  notify('info', 'Task added');
}

async function deleteTask(id) {
  await fetch(API + '/api/tasks/' + id, { method: 'DELETE' });
}

async function retryTask(id) {
  await fetch(API + '/api/tasks/' + id + '/retry', { method: 'POST' });
  notify('info', 'Task queued for retry');
}

async function startWorker(name) {
  const projectDir = document.getElementById('projectDir').value.trim();
  if (!projectDir) {
    notify('error', 'Set project directory first');
    return;
  }
  if (!name) name = 'main';
  await fetch(API + '/api/workers/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, project_dir: projectDir }),
  });
  notify('info', `Worker '${name}' started`);
}

async function stopWorker(name) {
  await fetch(API + '/api/workers/' + name + '/stop', { method: 'POST' });
  notify('info', `Worker '${name}' stopped`);
}

async function stopAllWorkers() {
  for (const name of Object.keys(lastWorkers)) {
    await fetch(API + '/api/workers/' + name + '/stop', { method: 'POST' });
  }
  notify('info', 'All workers stopped');
}

function setFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });
  renderTasks();
}


// -- Log viewer --------------------------------------------------------------

async function toggleLog(id) {
  const el = document.getElementById('log-' + id);
  if (!el) return;
  if (el.style.display === 'block') {
    el.style.display = 'none';
    openLogs.delete(id);
  } else {
    el.style.display = 'block';
    openLogs.add(id);
    await refreshLog(id);
  }
}

async function refreshLog(id) {
  const el = document.getElementById('log-' + id);
  if (!el || el.style.display === 'none') return;
  try {
    const res = await fetch(API + '/api/tasks/' + id + '/log');
    const data = await res.json();
    el.textContent = data.log || '(empty)';
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.textContent = '(error loading log)';
  }
}


// -- Project info ------------------------------------------------------------

async function refreshProjectInfo() {
  const dir = document.getElementById('projectDir').value.trim();
  if (!dir) {
    document.getElementById('verifyDetail').textContent = 'set project dir above';
    return;
  }
  try {
    const res = await fetch(API + '/api/verify-status?project_dir=' + encodeURIComponent(dir));
    const data = await res.json();
    const el = document.getElementById('verifyDetail');
    if (data.type === 'verify.sh') {
      el.textContent = 'verify.sh found';
      el.style.color = 'var(--accent-green)';
    } else if (data.type === 'auto-tests') {
      el.textContent = 'Tests: ' + data.detail;
      el.style.color = 'var(--accent-yellow)';
    } else {
      el.textContent = data.detail;
      el.style.color = 'var(--accent-red)';
    }
  } catch (e) {
    document.getElementById('verifyDetail').textContent = 'error checking project';
  }

  // Also refresh git log
  try {
    const res = await fetch(API + '/api/git-log?project_dir=' + encodeURIComponent(dir));
    const data = await res.json();
    document.getElementById('gitLog').textContent = data.log || '(empty)';
  } catch (e) {}
}


// -- Notifications -----------------------------------------------------------

function notify(type, message) {
  // In-page toast
  const container = document.getElementById('toasts');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  toast.onclick = () => toast.remove();
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);

  // Browser notification for task completion/failure
  if ((type === 'success' || type === 'error') &&
      'Notification' in window &&
      Notification.permission === 'granted') {
    new Notification('CC Autonomous', { body: message });
  }
}


// -- Utilities ---------------------------------------------------------------

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function formatElapsed(start, end) {
  const s = new Date(start);
  const e = end ? new Date(end) : new Date();
  const diff = Math.floor((e - s) / 1000);
  if (diff < 60) return `${diff}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ${diff % 60}s`;
  return `${Math.floor(diff / 3600)}h ${Math.floor((diff % 3600) / 60)}m`;
}

let debounceTimers = {};
function debounce(fn, ms) {
  return (...args) => {
    clearTimeout(debounceTimers[fn.name]);
    debounceTimers[fn.name] = setTimeout(() => fn(...args), ms);
  };
}
