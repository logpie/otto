"""
CC Autonomous Agent Manager v2 — Web UI with verify-fix loop.

Run:
    python3 manager.py

Then open http://localhost:8420 on your phone or browser.
"""

import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

app = FastAPI(title="CC Autonomous")

BASE_DIR = Path(__file__).parent
TASKS_FILE = BASE_DIR / "tasks.json"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

workers: dict[str, subprocess.Popen] = {}


def load_tasks() -> list[dict]:
    """Read-only task load. For mutations use _locked_task_rw_manager."""
    if not TASKS_FILE.exists():
        return []
    with open(TASKS_FILE) as f:
        return json.load(f)


def _locked_task_rw_manager(mutator):
    """Same transaction pattern as worker's _locked_task_rw.

    All manager mutations (add, delete, retry) MUST use this instead of
    separate load_tasks()/save_tasks() to prevent lost updates under
    concurrent access.
    """
    import fcntl
    import tempfile
    lock_path = TASKS_FILE.with_suffix(".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = json.loads(TASKS_FILE.read_text()) if TASKS_FILE.exists() else []
        result = mutator(tasks)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(TASKS_FILE.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(tasks, f, indent=2)
            os.replace(tmp_path, str(TASKS_FILE))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return result


# ── API ──────────────────────────────────────────────────────────────────────


@app.get("/api/tasks")
def get_tasks():
    return load_tasks()


@app.post("/api/tasks")
async def add_task(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, 400)

    # Support both old 'verify' field and new split fields
    verify_prompt = (
        body.get("verify_prompt", "").strip()
        or body.get("verify", "").strip()
    )
    verify_cmd = body.get("verify_cmd", "").strip()

    try:
        max_retries = int(body.get("max_retries", 3))
        max_retries = max(1, min(max_retries, 10))  # clamp to [1, 10]
    except (ValueError, TypeError):
        return JSONResponse({"error": "max_retries must be an integer 1-10"}, 400)

    task = {
        "id": uuid.uuid4().hex[:8],
        "prompt": prompt,
        "verify_prompt": verify_prompt,
        "verify_cmd": verify_cmd,
        "max_retries": max_retries,
        "status": "pending",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "started_at": None,
        "finished_at": None,
        "worker": None,
        "attempts": 0,
        "cost_usd": 0.0,
        "session_id": None,
    }

    def _add(tasks):
        tasks.append(task)
    _locked_task_rw_manager(_add)
    return task


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    def _delete(tasks):
        tasks[:] = [t for t in tasks if t["id"] != task_id]
    _locked_task_rw_manager(_delete)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    def _retry(tasks):
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "pending"
                t["started_at"] = None
                t["finished_at"] = None
                t["worker"] = None
                t["attempts"] = 0
                t["cost_usd"] = 0.0
                t["session_id"] = None
                t.pop("last_error", None)
                break
    _locked_task_rw_manager(_retry)
    return {"ok": True}


@app.get("/api/tasks/{task_id}/log")
def get_task_log(task_id: str):
    log_file = LOGS_DIR / f"{task_id}.log"
    if not log_file.exists():
        return {"log": "(no log yet)"}

    lines = []
    try:
        for line in log_file.read_text().splitlines():
            try:
                entry = json.loads(line)
                if entry.get("type") == "assistant" and "message" in entry:
                    msg = entry["message"]
                    if isinstance(msg, dict) and "content" in msg:
                        for block in msg["content"]:
                            if isinstance(block, dict) and block.get("type") == "text":
                                lines.append(block["text"])
                    elif isinstance(msg, str):
                        lines.append(msg)
            except json.JSONDecodeError:
                lines.append(line)
    except Exception:
        lines.append(log_file.read_text()[-5000:])

    return {"log": "\n".join(lines[-200:])}


@app.get("/api/workers")
def get_workers():
    alive = {}
    for name, proc in list(workers.items()):
        if proc.poll() is None:
            alive[name] = {"pid": proc.pid, "status": "running"}
        else:
            alive[name] = {"pid": proc.pid, "status": "exited", "code": proc.returncode}
    return alive


@app.post("/api/workers/start")
async def start_worker(request: Request):
    body = await request.json()
    name = body.get("name", "main")
    project_dir = body.get("project_dir", str(BASE_DIR))
    worktree = body.get("worktree", False)
    max_retries = body.get("max_retries", 3)

    if name in workers and workers[name].poll() is None:
        return JSONResponse({"error": f"Worker '{name}' already running"}, 400)

    cmd = [
        "bash", str(BASE_DIR / "ralph-loop.sh"),
        "--project-dir", project_dir,
        "--max-retries", str(max_retries),
    ]
    if worktree:
        cmd.extend(["--worktree", name])

    log_path = LOGS_DIR / f"worker-{name}.log"
    log_fh = open(log_path, "a")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, env=env)
    workers[name] = proc
    return {"name": name, "pid": proc.pid, "status": "started"}


@app.post("/api/workers/{name}/stop")
def stop_worker(name: str):
    if name not in workers:
        return JSONResponse({"error": "Worker not found"}, 404)
    proc = workers[name]
    if proc.poll() is None:
        proc.terminate()
    return {"name": name, "status": "stopped"}


@app.get("/api/git/log")
def git_log(project_dir: str = ""):
    if not project_dir:
        return {"log": "(set project directory)"}
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5,
        )
        return {"log": result.stdout.strip()}
    except Exception as e:
        return {"log": f"error: {e}"}


@app.get("/api/verify-status")
def verify_status(project_dir: str = ""):
    """Check if project has a verify.sh or auto-detectable tests."""
    if not project_dir:
        return {"type": "none", "detail": "no project dir"}
    p = Path(project_dir)
    if (p / "verify.sh").exists():
        return {"type": "verify.sh", "detail": (p / "verify.sh").read_text()[:500]}
    test_files = list(p.glob("test_*.py")) + list(p.glob("*_test.py"))
    if test_files:
        return {"type": "auto-tests", "detail": ", ".join(f.name for f in test_files[:10])}
    return {"type": "none", "detail": "No verify.sh or test files found."}


@app.get("/api/events")
async def events():
    """Server-Sent Events endpoint for live task/worker updates."""
    async def event_stream():
        last_data = ""
        try:
            while True:
                tasks = load_tasks()
                workers_status = {}
                for name, proc in list(workers.items()):
                    poll = proc.poll()
                    workers_status[name] = {
                        "pid": proc.pid,
                        "status": "running" if poll is None else "exited",
                        "code": poll,
                    }
                current_data = json.dumps(
                    {"tasks": tasks, "workers": workers_status},
                    default=str,
                )
                if current_data != last_data:
                    yield f"data: {current_data}\n\n"
                    last_data = current_data
                await asyncio.sleep(1)
        except (asyncio.CancelledError, GeneratorExit):
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Web UI ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="CC Auto">
<title>CC Autonomous</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #0d1117; color: #c9d1d9; padding: 16px; max-width: 600px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 16px; color: #58a6ff; }
  .input-row { display: flex; gap: 8px; margin-bottom: 12px; }
  input[type=text], textarea { flex: 1; padding: 10px 12px; background: #161b22; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 16px; }
  textarea { min-height: 60px; resize: vertical; font-family: inherit; }
  button { padding: 10px 16px; background: #238636; border: none; border-radius: 6px; color: #fff; font-size: 14px; cursor: pointer; white-space: nowrap; }
  button:active { opacity: 0.8; }
  button.danger { background: #da3633; }
  button.secondary { background: #30363d; }
  .task { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; margin-bottom: 8px; }
  .task-prompt { font-size: 14px; word-break: break-word; }
  .task-meta { font-size: 12px; color: #8b949e; margin-top: 6px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge.pending { background: #1f2937; color: #9ca3af; }
  .badge.in_progress { background: #1e3a5f; color: #58a6ff; }
  .badge.completed { background: #0d2818; color: #3fb950; }
  .badge.failed { background: #3d1418; color: #f85149; }
  .badge.verifying { background: #3d2e00; color: #d29922; }
  .task-actions { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
  .task-actions button { padding: 4px 10px; font-size: 12px; }
  .log-box { background: #0d1117; border: 1px solid #30363d; border-radius: 6px; padding: 8px; margin-top: 8px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
  .section { margin-bottom: 20px; }
  .section h2 { font-size: 16px; margin-bottom: 8px; color: #8b949e; }
  #projectDir { width: 100%; margin-bottom: 8px; }
  .worker-controls { display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
  .info-box { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 10px; font-size: 12px; margin-bottom: 10px; }
  .info-box .label { color: #8b949e; }
  .info-box .value { color: #c9d1d9; }
  .git-log { font-family: monospace; font-size: 12px; color: #8b949e; white-space: pre; overflow-x: auto; max-height: 150px; overflow-y: auto; }
</style>
</head>
<body>
<h1>CC Autonomous <span style="font-size:12px;color:#8b949e">v2</span></h1>

<div class="section">
  <h2>Project</h2>
  <input type="text" id="projectDir" placeholder="Project directory (absolute path)" oninput="onProjectDirChange()">
  <div class="info-box" id="verifyInfo">
    <span class="label">Verification:</span> <span class="value" id="verifyDetail">set project dir above</span>
  </div>
</div>

<div class="section">
  <h2>Workers</h2>
  <div class="worker-controls">
    <button onclick="startWorker('main', false)">Start Worker</button>
    <button class="secondary" onclick="startWorker(prompt('Worker name:') || 'w1', true)">+ Worktree Worker</button>
    <button class="danger" onclick="stopAllWorkers()">Stop All</button>
  </div>
  <div id="workerList"></div>
</div>

<div class="section">
  <h2>Tasks</h2>
  <div>
    <textarea id="taskInput" placeholder="Describe the task..." rows="2" style="width:100%;margin-bottom:6px" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();addTask()}"></textarea>
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <input type="text" id="verifyInput" placeholder="Verify goal (optional, e.g. 'E2E latency < 500ms')" style="flex:1;font-size:13px">
      <button onclick="addTask()">Add</button>
    </div>
  </div>
  <div id="taskList"></div>
</div>

<div class="section">
  <h2>Git Log</h2>
  <div class="git-log" id="gitLog">set project dir above</div>
</div>

<script>
const API = '';
let projectDirTimer = null;

function onProjectDirChange() {
  clearTimeout(projectDirTimer);
  projectDirTimer = setTimeout(() => {
    refreshVerify();
    refreshGitLog();
  }, 500);
}

async function refreshVerify() {
  const dir = document.getElementById('projectDir').value.trim();
  if (!dir) { document.getElementById('verifyDetail').textContent = 'set project dir above'; return; }
  const res = await fetch(API + '/api/verify-status?project_dir=' + encodeURIComponent(dir));
  const data = await res.json();
  const el = document.getElementById('verifyDetail');
  if (data.type === 'verify.sh') {
    el.textContent = 'verify.sh found';
    el.style.color = '#3fb950';
  } else if (data.type === 'auto-tests') {
    el.textContent = 'auto-detect tests: ' + data.detail;
    el.style.color = '#d29922';
  } else {
    el.textContent = data.detail;
    el.style.color = '#f85149';
  }
}

async function refreshGitLog() {
  const dir = document.getElementById('projectDir').value.trim();
  if (!dir) return;
  const res = await fetch(API + '/api/git-log?project_dir=' + encodeURIComponent(dir));
  const data = await res.json();
  document.getElementById('gitLog').textContent = data.log || '(empty)';
}

async function addTask() {
  const input = document.getElementById('taskInput');
  const verifyInput = document.getElementById('verifyInput');
  const prompt = input.value.trim();
  const verify = verifyInput.value.trim();
  if (!prompt) return;
  await fetch(API + '/api/tasks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({prompt, verify})
  });
  input.value = '';
  verifyInput.value = '';
  refresh();
}

async function deleteTask(id) {
  await fetch(API + '/api/tasks/' + id, {method: 'DELETE'});
  refresh();
}

async function retryTask(id) {
  await fetch(API + '/api/tasks/' + id + '/retry', {method: 'POST'});
  refresh();
}

const openLogs = new Set();

async function viewLog(id) {
  const el = document.getElementById('log-' + id);
  if (el.style.display === 'block') { el.style.display = 'none'; openLogs.delete(id); return; }
  const res = await fetch(API + '/api/tasks/' + id + '/log');
  const data = await res.json();
  el.textContent = data.log || '(empty)';
  el.style.display = 'block';
  el.scrollTop = el.scrollHeight;
  openLogs.add(id);
}

async function startWorker(name, worktree) {
  const projectDir = document.getElementById('projectDir').value.trim();
  if (!projectDir) { alert('Set project directory first'); return; }
  await fetch(API + '/api/workers/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, project_dir: projectDir, worktree})
  });
  refresh();
}

async function stopWorker(name) {
  await fetch(API + '/api/workers/' + name + '/stop', {method: 'POST'});
  refresh();
}

async function stopAllWorkers() {
  const res = await fetch(API + '/api/workers');
  const workers = await res.json();
  for (const name of Object.keys(workers)) {
    await fetch(API + '/api/workers/' + name + '/stop', {method: 'POST'});
  }
  refresh();
}

async function refresh() {
  // Tasks
  const taskRes = await fetch(API + '/api/tasks');
  const tasks = await taskRes.json();
  const taskList = document.getElementById('taskList');
  taskList.innerHTML = tasks.slice().reverse().map(t => {
    const attempts = t.attempts || 0;
    const attemptStr = attempts > 0 ? ' (attempt ' + attempts + '/' + (t.max_retries || 3) + ')' : '';
    return `
    <div class="task">
      <div class="task-prompt">${esc(t.prompt)}</div>
      ${t.verify_prompt ? '<div style="font-size:12px;color:#d29922;margin-top:4px">Verify: ' + esc(t.verify_prompt) + '</div>' : ''}
      <div class="task-meta">
        <span class="badge ${t.status}">${t.status}${attemptStr}</span>
        <span>${t.id}</span>
        ${t.worker ? '<span>worker: ' + esc(t.worker) + '</span>' : ''}
        ${t.cost_usd ? '<span>$' + t.cost_usd.toFixed(4) + '</span>' : ''}
      </div>
      <div class="task-actions">
        <button class="secondary" onclick="viewLog('${t.id}')">Log</button>
        ${['failed','completed'].includes(t.status) ? '<button onclick="retryTask(\\''+t.id+'\\')">Retry</button>' : ''}
        <button class="danger" onclick="deleteTask('${t.id}')">Delete</button>
      </div>
      <div class="log-box" id="log-${t.id}" style="display:none"></div>
    </div>`;
  }).join('');

  // Restore open logs
  for (const id of openLogs) {
    const el = document.getElementById('log-' + id);
    if (el) {
      el.style.display = 'block';
      // Re-fetch log content
      fetch(API + '/api/tasks/' + id + '/log')
        .then(r => r.json())
        .then(data => { el.textContent = data.log || '(empty)'; el.scrollTop = el.scrollHeight; });
    }
  }

  // Workers
  const wRes = await fetch(API + '/api/workers');
  const workers = await wRes.json();
  const wList = document.getElementById('workerList');
  const entries = Object.entries(workers);
  if (entries.length === 0) {
    wList.innerHTML = '<div style="color:#8b949e;font-size:13px">No workers running</div>';
  } else {
    wList.innerHTML = entries.map(([name, w]) => `
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:4px;font-size:13px">
        <span class="badge ${w.status === 'running' ? 'in_progress' : 'completed'}">${w.status}</span>
        <span>${esc(name)} (pid ${w.pid})</span>
        ${w.status === 'running' ? '<button class="danger" style="padding:2px 8px;font-size:11px" onclick="stopWorker(\\''+name+'\\')">Stop</button>' : ''}
      </div>
    `).join('');
  }

  refreshGitLog();
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

// Restore project dir from localStorage
const saved = localStorage.getItem('cc-auto-project-dir');
if (saved) document.getElementById('projectDir').value = saved;
document.getElementById('projectDir').addEventListener('input', e => {
  localStorage.setItem('cc-auto-project-dir', e.target.value);
});

refresh();
refreshVerify();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


@app.get("/api/git-log")
def git_log_endpoint(project_dir: str = ""):
    if not project_dir:
        return {"log": "(set project directory)"}
    try:
        result = subprocess.run(
            ["git", "-C", project_dir, "log", "--oneline", "-20"],
            capture_output=True, text=True, timeout=5,
        )
        return {"log": result.stdout.strip()}
    except Exception as e:
        return {"log": f"error: {e}"}


@app.on_event("shutdown")
def shutdown_event():
    """Kill all worker subprocesses on shutdown."""
    for name, proc in workers.items():
        if proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    import signal
    import threading
    import uvicorn

    # Run uvicorn in a daemon thread — main thread only handles signals.
    # This prevents asyncio.run() from overriding our SIGINT handler.
    config = uvicorn.Config(app, host="0.0.0.0", port=8420, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    def handle_exit(sig, frame):
        print("\n[manager] Shutting down...")
        for name, proc in workers.items():
            if proc.poll() is None:
                proc.terminate()
        os._exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Main thread: just wait. Signals will interrupt this.
    thread.join()
