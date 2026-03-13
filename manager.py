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
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from verify_utils import _detect_verification

# Module-level defaults (used by create_app when no overrides given)
BASE_DIR = Path(__file__).parent
TASKS_FILE = BASE_DIR / "tasks.json"
LOGS_DIR = BASE_DIR / "logs"


def _locked_task_rw_manager(tasks_file: Path, mutator):
    """Same transaction pattern as worker's _locked_task_rw.

    All manager mutations (add, delete, retry) MUST use this instead of
    separate load_tasks()/save_tasks() to prevent lost updates under
    concurrent access.
    """
    import fcntl
    import tempfile
    # Keep the lockfile naming identical to worker.py: tasks.json -> tasks.lock.
    lock_path = tasks_file.with_suffix(".lock")
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        tasks = json.loads(tasks_file.read_text()) if tasks_file.exists() else []
        result = mutator(tasks)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(tasks_file.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(tasks, f, indent=2)
            os.replace(tmp_path, str(tasks_file))
        except BaseException:
            os.unlink(tmp_path)
            raise
        return result


def _reset_task_for_requeue(task: dict) -> None:
    task["status"] = "pending"
    task["started_at"] = None
    task["finished_at"] = None
    task["heartbeat_at"] = None
    task["worker"] = None
    task["attempts"] = 0
    task["cost_usd"] = 0.0
    task["session_id"] = None
    task.pop("last_error", None)


def _requeue_worker_tasks(tasks: list[dict], worker_name: str) -> int:
    requeued = 0
    for task in tasks:
        if (
            task.get("status") == "in_progress"
            and task.get("worker") == worker_name
        ):
            _reset_task_for_requeue(task)
            requeued += 1
    return requeued


def create_app(
    base_dir: Path | None = None,
    tasks_file: Path | None = None,
    logs_dir: Path | None = None,
) -> FastAPI:
    _base = base_dir or BASE_DIR
    _tasks = tasks_file or TASKS_FILE
    _logs = logs_dir or LOGS_DIR
    _logs.mkdir(exist_ok=True)

    _app = FastAPI(title="CC Autonomous")
    _app.state.base_dir = _base
    _app.state.tasks_file = _tasks
    _app.state.logs_dir = _logs
    _app.state.workers = {}
    _app.state.worker_dirs = {}

    # Mount static files
    static_dir = _base / "static"
    if static_dir.exists():
        _app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ── API ──────────────────────────────────────────────────────────────────

    @_app.get("/api/tasks")
    def get_tasks(request: Request):
        tf = request.app.state.tasks_file
        if not tf.exists():
            return []
        return json.loads(tf.read_text())

    @_app.post("/api/tasks")
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
            "heartbeat_at": None,
            "worker": None,
            "attempts": 0,
            "cost_usd": 0.0,
            "session_id": None,
        }

        def _add(tasks):
            tasks.append(task)
        _locked_task_rw_manager(request.app.state.tasks_file, _add)
        return task

    @_app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: str, request: Request):
        def _delete(tasks):
            for idx, task in enumerate(tasks):
                if task["id"] != task_id:
                    continue
                if task.get("status") == "in_progress":
                    return False
                del tasks[idx]
                break
            return True

        deleted = _locked_task_rw_manager(request.app.state.tasks_file, _delete)
        if not deleted:
            return JSONResponse({"error": "task is currently in_progress"}, 409)
        return {"ok": True}

    @_app.patch("/api/tasks/{task_id}")
    async def update_task_endpoint(task_id: str, request: Request):
        body = await request.json()
        allowed = {"verify_prompt", "verify_cmd", "max_retries"}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return JSONResponse({"error": "no valid fields to update"}, 400)

        def _update(tasks):
            for t in tasks:
                if t["id"] == task_id:
                    t.update(updates)
                    return t
            return None

        result = _locked_task_rw_manager(request.app.state.tasks_file, _update)
        if result is None:
            return JSONResponse({"error": "task not found"}, 404)
        return result

    @_app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str, request: Request):
        def _retry(tasks):
            for t in tasks:
                if t["id"] == task_id:
                    if t.get("status") == "in_progress":
                        return False
                    _reset_task_for_requeue(t)
                    break
            return True

        retried = _locked_task_rw_manager(request.app.state.tasks_file, _retry)
        if not retried:
            return JSONResponse({"error": "task is currently in_progress"}, 409)
        return {"ok": True}

    @_app.get("/api/tasks/{task_id}/log")
    def get_task_log(task_id: str, request: Request):
        log_file = request.app.state.logs_dir / f"{task_id}.log"
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

    @_app.get("/api/workers")
    def get_workers(request: Request):
        workers = request.app.state.workers
        alive = {}
        for name, proc in list(workers.items()):
            if proc.poll() is None:
                alive[name] = {"pid": proc.pid, "status": "running"}
            else:
                alive[name] = {"pid": proc.pid, "status": "exited", "code": proc.returncode}
        return alive

    @_app.post("/api/workers/start")
    async def start_worker(request: Request):
        body = await request.json()
        name = body.get("name", "main")
        project_dir = str(Path(body.get("project_dir", str(request.app.state.base_dir))).resolve())
        max_retries = body.get("max_retries", 3)

        workers = request.app.state.workers
        worker_dirs = request.app.state.worker_dirs

        # Clean up exited workers from worker_dirs
        for wname in list(worker_dirs):
            if wname not in workers or workers[wname].poll() is not None:
                worker_dirs.pop(wname, None)

        if name in workers and workers[name].poll() is None:
            return JSONResponse({"error": f"Worker '{name}' already running"}, 400)

        # Prevent multiple workers on same project_dir
        for wname, proc in workers.items():
            if proc.poll() is None and worker_dirs.get(wname) == project_dir:
                return JSONResponse(
                    {"error": f"Worker '{wname}' already running on {project_dir}"},
                    400,
                )

        log_path = request.app.state.logs_dir / f"worker-{name}.log"
        cmd = [
            sys.executable,
            str(request.app.state.base_dir / "worker.py"),
            "--tasks-file", str(request.app.state.tasks_file),
            "--project-dir", project_dir,
            "--worker-name", name,
            "--max-retries", str(max_retries),
            "--log-file", str(log_path),
        ]

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
        )
        workers[name] = proc
        worker_dirs[name] = project_dir
        return {"name": name, "pid": proc.pid, "status": "started"}

    @_app.post("/api/workers/{name}/stop")
    def stop_worker(name: str, request: Request):
        workers = request.app.state.workers
        if name not in workers:
            return JSONResponse({"error": "Worker not found"}, 404)
        proc = workers[name]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        requeued = _locked_task_rw_manager(
            request.app.state.tasks_file,
            lambda tasks: _requeue_worker_tasks(tasks, name),
        )
        return {
            "name": name,
            "status": "stopped",
            "requeued_tasks": requeued,
        }

    @_app.get("/api/git/log")
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

    @_app.get("/api/git-log")
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

    @_app.get("/api/verify-status")
    def verify_status(project_dir: str = ""):
        """Check if project has a verify.sh or auto-detectable tests."""
        if not project_dir:
            return {"type": "none", "detail": "no project dir"}
        return _detect_verification(Path(project_dir))

    @_app.get("/api/events")
    async def events(request: Request):
        """Server-Sent Events endpoint for live task/worker updates."""
        async def event_stream():
            last_data = ""
            try:
                while True:
                    tf = request.app.state.tasks_file
                    tasks = json.loads(tf.read_text()) if tf.exists() else []
                    workers_state = request.app.state.workers
                    workers_status = {}
                    for name, proc in list(workers_state.items()):
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

    # ── Web UI ───────────────────────────────────────────────────────────────

    @_app.get("/", response_class=HTMLResponse)
    def index():
        html_path = _app.state.base_dir / "static" / "index.html"
        if html_path.exists():
            return html_path.read_text()
        return "<h1>CC Autonomous v2</h1><p>static/index.html not found</p>"

    def _stop_all_workers_and_requeue():
        """Terminate all workers, wait for death, requeue their tasks."""
        for name, proc in list(_app.state.workers.items()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            _locked_task_rw_manager(
                _app.state.tasks_file,
                lambda tasks, wn=name: _requeue_worker_tasks(tasks, wn),
            )

    @_app.on_event("shutdown")
    def shutdown_event():
        """Kill all worker subprocesses and requeue their tasks on shutdown."""
        _stop_all_workers_and_requeue()

    return _app


# Module-level app for uvicorn
app = create_app()


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
        for name, proc in list(app.state.workers.items()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            _locked_task_rw_manager(
                app.state.tasks_file,
                lambda tasks, wn=name: _requeue_worker_tasks(tasks, wn),
            )
        os._exit(0)

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    # Main thread: just wait. Signals will interrupt this.
    thread.join()
