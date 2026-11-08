"""Local HTTP service the Jellyfin plugin talks to.

Jobs run one at a time on a single worker thread. The Whisper model is loaded
when a job starts and unloaded after ``--idle-unload`` seconds with an empty
queue, so the GPU can go back to idle between jobs.

Endpoints:
  GET  /health
  POST /jobs            {"path": "...", "overwrite": false, "item_id": "..."}
  GET  /jobs            newest first
  GET  /jobs/{id}
  GET  /history?path=   last result recorded for a file (survives restarts)
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import __version__
from .pipeline import ModelHolder, Settings, SkipItem, generate_forced_subtitles

log = logging.getLogger("forced_subs")


class JobRequest(BaseModel):
    path: str
    overwrite: bool = False
    item_id: Optional[str] = None


class History:
    """Remembers the last result per file so library sweeps don't redo work."""

    def __init__(self, file: Path):
        self.file = file
        self._lock = threading.Lock()
        try:
            self._data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    @staticmethod
    def _key(path: str) -> str:
        return str(Path(path)).lower()

    def get(self, path: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(self._key(path))

    def put(self, path: str, record: dict) -> None:
        with self._lock:
            self._data[self._key(path)] = record
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            tmp.replace(self.file)


class JobRunner:
    def __init__(self, settings: Settings, history: History, idle_unload: float):
        self.settings = settings
        self.models = ModelHolder(settings)
        self.history = history
        self.idle_unload = idle_unload
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.q: "queue.Queue[str]" = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="forced-subs-worker").start()

    def submit(self, req: JobRequest) -> dict:
        path = str(Path(req.path))
        with self.lock:
            for jid in reversed(self.order):
                j = self.jobs[jid]
                if j["path"].lower() == path.lower() and j["status"] in ("queued", "running"):
                    return dict(j)
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "path": path, "item_id": req.item_id, "overwrite": req.overwrite,
                "status": "queued", "stage": "Waiting", "progress": 0.0, "message": "",
                "output": None, "created": time.time(), "started": None, "finished": None,
            }
            self.jobs[jid] = job
            self.order.append(jid)
            # Keep memory bounded.
            while len(self.order) > 500:
                old = self.order.pop(0)
                if self.jobs[old]["status"] not in ("queued", "running"):
                    del self.jobs[old]
                else:
                    self.order.insert(0, old)
                    break
        self.q.put(jid)
        return dict(job)

    def list(self, limit: int) -> list[dict]:
        with self.lock:
            return [dict(self.jobs[j]) for j in reversed(self.order[-limit:])]

    def get(self, jid: str) -> Optional[dict]:
        with self.lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    def queued(self) -> int:
        return self.q.qsize()

    def _update(self, jid: str, **kw) -> None:
        with self.lock:
            self.jobs[jid].update(kw)

    def _worker(self) -> None:
        while True:
            try:
                jid = self.q.get(timeout=self.idle_unload)
            except queue.Empty:
                if self.models.loaded:
                    self.models.unload()
                continue
            job = self.get(jid)
            if job is None:
                continue
            self._update(jid, status="running", started=time.time(), stage="Starting")

            def progress(stage: str, fraction: float, _jid=jid) -> None:
                self._update(_jid, stage=stage, progress=round(float(fraction), 3))

            try:
                res = generate_forced_subtitles(Path(job["path"]), self.models, self.settings,
                                                overwrite=job["overwrite"], progress=progress)
                status = "skipped" if res.status == "skipped" else "done"
                self._update(jid, status=status, stage=res.status, progress=1.0,
                             message=res.message, output=res.output, result=res.to_dict())
                log.info("%s: %s", Path(job["path"]).name, res.message)
            except SkipItem as e:
                res = None
                self._update(jid, status="skipped", stage="skipped", message=str(e))
            except Exception as e:  # noqa: BLE001 - report any failure to the caller
                res = None
                log.exception("Job %s failed", jid)
                self._update(jid, status="failed", stage="failed", message=f"{type(e).__name__}: {e}")
            finally:
                self._update(jid, finished=time.time())
                done = self.get(jid)
                self.history.put(done["path"], {
                    "status": done["status"], "stage": done["stage"], "message": done["message"],
                    "output": done["output"], "finished": done["finished"],
                })


def create_app(runner: JobRunner) -> FastAPI:
    app = FastAPI(title="Forced Subtitles", version=__version__)

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "model": runner.settings.model,
                "model_loaded": runner.models.loaded, "queued": runner.queued()}

    @app.post("/jobs")
    def submit(req: JobRequest):
        if not Path(req.path).is_file():
            raise HTTPException(404, f"File not found on this machine: {req.path}")
        return runner.submit(req)

    @app.get("/jobs")
    def jobs(limit: int = 50):
        return runner.list(max(1, min(limit, 500)))

    @app.get("/jobs/{jid}")
    def job(jid: str):
        j = runner.get(jid)
        if j is None:
            raise HTTPException(404, "No such job")
        return j

    @app.get("/history")
    def history(path: str):
        return runner.history.get(path) or {}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Forced subtitle service for Jellyfin")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--model", default=Settings.model)
    ap.add_argument("--device", default=Settings.device)
    ap.add_argument("--compute-type", default=Settings.compute_type)
    ap.add_argument("--idle-unload", type=float, default=60.0,
                    help="Seconds with no jobs before the model is unloaded from the GPU")
    ap.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    args = ap.parse_args()

    data = Path(args.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(data / "service.log", encoding="utf-8")],
    )
    settings = Settings(model=args.model, device=args.device, compute_type=args.compute_type)
    runner = JobRunner(settings, History(data / "history.json"), args.idle_unload)
    uvicorn.run(create_app(runner), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
