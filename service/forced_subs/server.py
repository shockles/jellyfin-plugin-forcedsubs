"""Local HTTP service the Jellyfin plugin talks to.

Jobs run one at a time on a single worker thread. The Whisper model is loaded
when a job starts and unloaded after ``idle_unload`` seconds with an empty
queue, so the GPU can go back to idle between jobs.

Endpoints (all but /health need the X-Api-Key header when --api-key is set):
  GET    /health
  GET    /settings              default settings (what the plugin can override)
  POST   /jobs                  {"path": "...", "overwrite": false, "item_id": "...", "options": {...}}
  GET    /jobs                  newest first
  GET    /jobs/{id}
  POST   /jobs/{id}/cancel
  GET    /history?path=         last result recorded for a file (survives restarts)
  DELETE /history?path=         forget one file (the library task will check it again)
  DELETE /history               forget everything
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import queue
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .pipeline import Cancelled, ModelHolder, Settings, SkipItem, generate_subtitles

log = logging.getLogger("forced_subs")

ACTIVE = ("queued", "running")


class JobRequest(BaseModel):
    path: str
    overwrite: bool = False
    item_id: Optional[str] = None
    options: dict = Field(default_factory=dict)
    signature: Optional[str] = None  # what was asked for; the plugin redoes items when it changes


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
        return os.path.normcase(os.path.normpath(path))

    def _save(self) -> None:
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
        tmp.replace(self.file)

    def get(self, path: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(self._key(path))

    def put(self, path: str, record: dict) -> None:
        with self._lock:
            self._data[self._key(path)] = record
            self._save()

    def delete(self, path: Optional[str]) -> int:
        with self._lock:
            if path is None:
                n = len(self._data)
                self._data = {}
            else:
                n = 1 if self._data.pop(self._key(path), None) is not None else 0
            self._save()
            return n


class JobRunner:
    def __init__(self, defaults: Settings, history: History, idle_unload: float):
        self.defaults = defaults
        self.models = ModelHolder()
        self.history = history
        self.idle_unload = idle_unload
        self.jobs: dict[str, dict] = {}
        self.order: list[str] = []
        self.cancel_flags: set[str] = set()
        self.q: "queue.Queue[str]" = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="forced-subs-worker").start()

    def submit(self, req: JobRequest) -> dict:
        path = str(Path(req.path))
        key = os.path.normcase(path)
        with self.lock:
            for jid in reversed(self.order):
                j = self.jobs[jid]
                if os.path.normcase(j["path"]) == key and j["status"] in ACTIVE:
                    return self._public(j)
            jid = uuid.uuid4().hex[:12]
            job = {
                "id": jid, "path": path, "item_id": req.item_id, "overwrite": req.overwrite,
                "options": req.options, "signature": req.signature, "status": "queued", "stage": "Waiting", "progress": 0.0,
                "message": "", "output": None, "created": time.time(), "started": None, "finished": None,
            }
            self.jobs[jid] = job
            self.order.append(jid)
            self._trim()
        self.q.put(jid)
        return self._public(job)

    def _trim(self) -> None:
        # Keep the 500 most recent jobs; never drop active ones.
        excess = len(self.order) - 500
        if excess <= 0:
            return
        keep = []
        for jid in self.order:
            if excess > 0 and self.jobs[jid]["status"] not in ACTIVE:
                del self.jobs[jid]
                excess -= 1
            else:
                keep.append(jid)
        self.order = keep

    @staticmethod
    def _public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "options"}

    def list(self, limit: int) -> list[dict]:
        with self.lock:
            return [self._public(self.jobs[j]) for j in reversed(self.order[-limit:])]

    def get(self, jid: str) -> Optional[dict]:
        with self.lock:
            j = self.jobs.get(jid)
            return self._public(j) if j else None

    def cancel(self, jid: str) -> Optional[dict]:
        with self.lock:
            j = self.jobs.get(jid)
            if j is None:
                return None
            if j["status"] == "queued":
                j.update(status="cancelled", stage="cancelled", message="Cancelled", finished=time.time())
            elif j["status"] == "running":
                self.cancel_flags.add(jid)
                j["stage"] = "Cancelling"
            return self._public(j)

    def queued(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j["status"] in ACTIVE)

    def _update(self, jid: str, **kw) -> None:
        with self.lock:
            self.jobs[jid].update(kw)

    def _worker(self) -> None:
        while True:
            try:
                jid = self.q.get(timeout=max(1.0, self.idle_unload))
            except queue.Empty:
                if self.models.loaded:
                    self.models.unload()
                continue
            with self.lock:
                job = self.jobs.get(jid)
                if job is None or job["status"] != "queued":
                    continue  # cancelled or trimmed while waiting
                job.update(status="running", started=time.time(), stage="Starting")
                options = dict(job["options"] or {})
                path, overwrite = job["path"], job["overwrite"]

            if "idle_unload" in options:
                try:
                    self.idle_unload = max(5.0, float(options["idle_unload"]))
                except (TypeError, ValueError):
                    pass
            settings = self.defaults.with_options(options)

            def progress(stage: str, fraction: float, _jid=jid) -> None:
                if _jid in self.cancel_flags:
                    raise Cancelled()
                self._update(_jid, stage=stage, progress=round(float(fraction), 3))

            try:
                res = generate_subtitles(Path(path), self.models, settings,
                                                overwrite=overwrite, progress=progress)
                status = "skipped" if res.status == "skipped" else "done"
                self._update(jid, status=status, stage=res.status, progress=1.0,
                             message=res.message, output=res.output, result=res.to_dict())
                log.info("%s: %s", Path(path).name, res.message)
            except Cancelled:
                self._update(jid, status="cancelled", stage="cancelled", message="Cancelled")
            except SkipItem as e:
                self._update(jid, status="skipped", stage="skipped", message=str(e))
            except Exception as e:  # noqa: BLE001 - report any failure to the caller
                log.exception("Job %s failed", jid)
                self._update(jid, status="failed", stage="failed", message=f"{type(e).__name__}: {e}")
            finally:
                self.cancel_flags.discard(jid)
                self._update(jid, finished=time.time())
                done = self.get(jid)
                if done and done["status"] != "cancelled":
                    try:
                        self.history.put(done["path"], {
                            "status": done["status"], "stage": done["stage"], "message": done["message"],
                            "output": done["output"], "finished": done["finished"],
                            "signature": done.get("signature"),
                        })
                    except Exception:  # noqa: BLE001 - a history write must never kill the worker
                        log.exception("Could not save history for %s", done["path"])


def create_app(runner: JobRunner, api_key: Optional[str]) -> FastAPI:
    app = FastAPI(title="Forced Subtitles", version=__version__)

    def auth(x_api_key: Optional[str] = Header(default=None)) -> None:
        if api_key and not (x_api_key and secrets.compare_digest(x_api_key, api_key)):
            raise HTTPException(401, "Missing or wrong X-Api-Key")

    @app.get("/health")
    def health():
        try:
            import ctranslate2
            gpus = ctranslate2.get_cuda_device_count()
        except Exception:  # noqa: BLE001
            gpus = 0
        key = runner.models.loaded_key
        return {"ok": True, "version": __version__, "model": runner.defaults.model,
                "model_loaded": runner.models.loaded, "loaded_model": key[0] if key else None,
                "cuda_devices": gpus, "queued": runner.queued(), "auth": bool(api_key)}

    @app.get("/settings", dependencies=[Depends(auth)])
    def settings():
        return dataclasses.asdict(runner.defaults) | {"idle_unload": runner.idle_unload}

    @app.post("/jobs", dependencies=[Depends(auth)])
    def submit(req: JobRequest):
        if not Path(req.path).is_file():
            raise HTTPException(404, f"File not found on the service machine: {req.path}")
        return runner.submit(req)

    @app.get("/jobs", dependencies=[Depends(auth)])
    def jobs(limit: int = 50):
        return runner.list(max(1, min(limit, 500)))

    @app.get("/jobs/{jid}", dependencies=[Depends(auth)])
    def job(jid: str):
        j = runner.get(jid)
        if j is None:
            raise HTTPException(404, "No such job")
        return j

    @app.post("/jobs/{jid}/cancel", dependencies=[Depends(auth)])
    def cancel(jid: str):
        j = runner.cancel(jid)
        if j is None:
            raise HTTPException(404, "No such job")
        return j

    @app.get("/history", dependencies=[Depends(auth)])
    def history(path: str):
        return runner.history.get(path) or {}

    @app.delete("/history", dependencies=[Depends(auth)])
    def forget(path: Optional[str] = None):
        return {"removed": runner.history.delete(path)}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Forced subtitle service for Jellyfin")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Use 0.0.0.0 to accept connections from other machines (set --api-key too)")
    ap.add_argument("--port", type=int, default=8796)
    ap.add_argument("--api-key", default=os.environ.get("FORCED_SUBS_API_KEY"),
                    help="Require this key in the X-Api-Key header (or set FORCED_SUBS_API_KEY)")
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
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.api_key:
        log.warning("Listening on %s without --api-key: anyone on the network can queue jobs.", args.host)
    defaults = Settings(model=args.model, device=args.device, compute_type=args.compute_type)
    runner = JobRunner(defaults, History(data / "history.json"), args.idle_unload)
    uvicorn.run(create_app(runner, args.api_key), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
