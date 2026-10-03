"""Private single-user Chroma studio. Bind only to loopback behind Tailscale Serve."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from runner import BusyError, DockerRunner, Settings, acquire_gpu_lock, read_png

TERMINAL = {"completed", "failed", "cancelled"}
ROOT = Path(__file__).resolve().parent
ERROR_MESSAGES = {
    "interrupted": "Le service a redémarré pendant la génération. Tu peux réessayer.",
    "yield_to_swarmer": "Une tâche Swarmer a demandé le GPU. Cette génération a été arrêtée ; tu peux la relancer ensuite.",
    "admission_unavailable": "Le contrôle des tâches Swarmer est indisponible. La génération a été arrêtée.",
    "deadline_exceeded": "La génération a dépassé sa limite de quinze minutes.",
    "renderer_memory_limit": "La génération a atteint sa limite mémoire.",
    "renderer_launch_failed": "Le moteur n’a pas pu démarrer. Le diagnostic est conservé sur le serveur.",
    "renderer_state_unavailable": "L’état du moteur est devenu indisponible. Son exécution a été arrêtée.",
    "renderer_failed": "Le moteur n’a pas produit d’image. Le diagnostic est conservé sur le serveur.",
    "renderer_cleanup_failed": "Le nettoyage du moteur n’est pas confirmé. Une intervention serveur est nécessaire.",
    "invalid_image_output": "Le résultat n’est pas une image PNG valide.",
    "unexpected_image_dimensions": "Les dimensions de l’image produite ne correspondent pas à la demande.",
}


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    prompt: str = Field(min_length=1, max_length=2000)
    negative_prompt: str = Field(default="blurry, low quality", max_length=2000)
    steps: StrictInt = Field(default=40, ge=1, le=40)
    seed: StrictInt | None = Field(default=42, ge=0, le=2147483647)
    width: StrictInt = Field(default=512, ge=512, le=512)
    height: StrictInt = Field(default=512, ge=512, le=512)

    @field_validator("prompt", "negative_prompt")
    @classmethod
    def clean_prompt(cls, text, info):
        if "\x00" in text:
            raise ValueError("NUL is not permitted")
        text = text.strip()
        if info.field_name == "prompt" and not text:
            raise ValueError("Prompt must not be blank")
        return text


class JobManager:
    def __init__(self, settings: Settings, runner):
        self.settings = settings
        self.runner = runner
        self.jobs: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.active: str | None = None
        self.cancel_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state_lock = None
        self.started_mono: dict[str, float] = {}
        self.blocked_reason: str | None = None
        self.gpu_lease: int | None = None

    def start(self):
        self.settings.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.settings.state, 0o700)
        self.state_lock = (self.settings.state / ".service.lock").open("a")
        try:
            fcntl.flock(self.state_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.runner.cleanup_interrupted()
            for path in sorted(self.settings.state.glob("*/job.json")):
                if path.is_symlink() or path.parent.is_symlink():
                    continue
                try:
                    job = json.loads(path.read_text())
                    if job["id"] != path.parent.name or not re.fullmatch(r"[a-f0-9]{32}", job["id"]):
                        continue
                    if job["status"] not in TERMINAL:
                        job.update(status="failed", phase="failed", progress=None, error=ERROR_MESSAGES["interrupted"])
                        self._save(job)
                    self.jobs[job["id"]] = job
                except (ValueError, KeyError, OSError):
                    continue
        except BaseException:
            self.state_lock.close()
            self.state_lock = None
            raise

    def close(self):
        self.cancel_event.set()
        if self.thread is not None:
            self.thread.join(timeout=45)
        # Lifespan shutdown only completes after own worker cleanup. Docker's hard
        # timeout is still the backstop if the host kills this process outright.
        if self.thread is not None and self.thread.is_alive():
            self.runner.cleanup_interrupted()
            self.thread.join(timeout=10)
        if self.gpu_lease is not None and (self.thread is None or not self.thread.is_alive()):
            # Cleanup uncertainty retains the shared lease until positively
            # resolved, so the worker cannot start another image meanwhile.
            self.runner.cleanup_interrupted()
            self._release_gpu_lease()
        if self.state_lock is not None:
            self.state_lock.close()

    def _release_gpu_lease(self):
        if self.gpu_lease is not None:
            os.close(self.gpu_lease)
            self.gpu_lease = None

    def _gpu_available(self):
        try:
            descriptor = acquire_gpu_lock(self.settings.gpu_lock)
        except BlockingIOError:
            return False, "Le GPU est réservé au worker de création d’images Swarmer.", "image_slot_reserved"
        except OSError:
            return False, "Le verrou partagé du GPU est indisponible.", "gpu_lock_unavailable"
        else:
            os.close(descriptor)
            return True, "", "ready"

    def _save(self, job):
        directory = self.settings.state / job["id"]
        directory.mkdir(mode=0o700, exist_ok=True)
        path = directory / "job.json"
        temp = directory / "job.json.tmp"
        with temp.open("w") as sink:
            json.dump(job, sink, ensure_ascii=False, indent=2)
            sink.flush()
            os.fsync(sink.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)

    def public(self, job):
        result = dict(job)
        if job["status"] not in TERMINAL and job["id"] in self.started_mono:
            result["elapsed_seconds"] = round(time.monotonic() - self.started_mono[job["id"]], 2)
        result.pop("image_sha256", None)
        return result

    def status(self):
        with self.lock:
            active = self.public(self.jobs[self.active]) if self.active else None
            recent = sorted(self.jobs.values(), key=lambda value: value["created_at"], reverse=True)[:50]
            jobs = [self.public(job) for job in recent]
        if self.blocked_reason:
            ready, message, availability_code = False, self.blocked_reason, "cleanup_required"
        else:
            ready, message, availability_code = (False, "Une génération est en cours.", "studio_active") if active else self._gpu_available()
            if ready:
                ready, message, availability_code = self.runner.ready()
        return {"active_job": active, "jobs": jobs, "ready": ready, "message": message,
                "availability_code": availability_code,
                "defaults": {"steps": 40, "seed": 42, "width": 512, "height": 512},
                "limits": {"min_steps": 1, "max_steps": 40, "max_prompt_length": 2000, "max_negative_prompt_length": 2000,
                           "max_seed": 2147483647, "width": 512, "height": 512, "max_active_jobs": 1, "deadline_seconds": 900}}

    def get(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise HTTPException(404, "Image introuvable.")
            return self.public(self.jobs[identifier])

    def create(self, request: JobRequest):
        with self.lock:
            if self.blocked_reason:
                raise HTTPException(503, self.blocked_reason)
            if self.active:
                raise HTTPException(409, "Une génération est déjà en cours.")
            ready, message, _ = self.runner.ready()
            if not ready:
                raise HTTPException(409, message)
            try:
                self.gpu_lease = acquire_gpu_lock(self.settings.gpu_lock)
            except BlockingIOError:
                raise HTTPException(409, "Le GPU est réservé au worker de création d’images Swarmer.") from None
            except OSError:
                raise HTTPException(503, "Le verrou partagé du GPU est indisponible.") from None
            identifier = uuid.uuid4().hex
            data = request.model_dump()
            data["seed"] = secrets.randbelow(2147483648) if request.seed is None else request.seed
            job = {**data, "id": identifier, "status": "queued", "phase": "queued", "progress": None,
                   "elapsed_seconds": 0, "created_at": datetime.now(timezone.utc).isoformat(), "image_url": None, "error": None}
            self.jobs[identifier] = job
            self.active = identifier
            self.started_mono[identifier] = time.monotonic()
            self.cancel_event = threading.Event()
            try:
                self._save(job)
                self.thread = threading.Thread(target=self._run, args=(identifier, self.cancel_event), daemon=True)
                self.thread.start()
            except BaseException:
                self._release_gpu_lease()
                self.active = None
                self.started_mono.pop(identifier, None)
                self.jobs.pop(identifier, None)
                raise
            return self.public(job)

    def _run(self, identifier, cancel):
        def update(**values):
            with self.lock:
                job = self.jobs[identifier]
                if job["status"] in TERMINAL:
                    return
                if cancel.is_set():
                    values["phase"] = "cancelling"
                job.update(values)
                job["elapsed_seconds"] = round(time.monotonic() - self.started_mono[identifier], 2)
                self._save(job)

        try:
            result = self.runner.run(dict(self.jobs[identifier]), self.settings.state / identifier, cancel, update)
        except BusyError:
            result = {"status": "failed", "phase": "failed", "error": "yield_to_swarmer"}
        except Exception as exc:  # noqa: BLE001 - Thread boundary persists a terminal job receipt.
            code = str(exc)
            if code not in ERROR_MESSAGES:
                code = "renderer_failed"
            result = {"status": "failed", "phase": "failed", "error": code}
        with self.lock:
            job = self.jobs[identifier]
            if result.get("error") == "renderer_cleanup_failed":
                self.blocked_reason = ERROR_MESSAGES["renderer_cleanup_failed"]
            result["error"] = ERROR_MESSAGES.get(result.get("error"), result.get("error"))
            job.update(result)
            job["elapsed_seconds"] = round(time.monotonic() - self.started_mono[identifier], 2)
            job["progress"] = 1.0 if job["status"] == "completed" else None
            job["image_url"] = f"/api/jobs/{identifier}/image" if job["status"] == "completed" else None
            try:
                self._save(job)
            finally:
                # runner.run returns only after container cleanup. If cleanup
                # could not be confirmed, retain the lease and fail closed.
                if not self.blocked_reason:
                    self._release_gpu_lease()
                self.active = None
                self.started_mono.pop(identifier, None)

    def cancel(self, identifier):
        with self.lock:
            job = self.get(identifier)
            if job["status"] in TERMINAL:
                return job
            if self.active != identifier:
                raise HTTPException(409, "Cette génération n’est plus active.")
            self.cancel_event.set()
            self.jobs[identifier]["phase"] = "cancelling"
            self._save(self.jobs[identifier])
            return self.public(self.jobs[identifier])


def create_app(settings=None, runner=None, allowed_hosts=None, origins=None):
    settings = settings or Settings.from_env()
    manager = JobManager(settings, runner or DockerRunner(settings))
    csrf_token = secrets.token_urlsafe(32)
    hosts = set(allowed_hosts or os.environ.get("CHROMA_STUDIO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(","))
    hosts = {host.strip().lower() for host in hosts if host.strip()}
    allowed_origins = set(origins or os.environ.get("CHROMA_STUDIO_ORIGINS", "http://127.0.0.1:8765,http://localhost:8765").split(","))

    @asynccontextmanager
    async def lifespan(app):
        manager.start()
        try:
            yield
        finally:
            manager.close()

    app = FastAPI(title="Chroma Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.manager = manager

    @app.middleware("http")
    async def private_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        try:
            parsed = urlsplit("//" + host)
            valid_host = parsed.hostname in hosts and parsed.username is None and not parsed.path
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse({"detail": "Host refusé."}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Requête externe refusée."}, status_code=403)
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return JSONResponse({"detail": "Origine refusée."}, status_code=403)
            token = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(token, csrf_token):
                return JSONResponse({"detail": "Session actualisée : recharge la page."}, status_code=403)
            if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                return JSONResponse({"detail": "JSON requis."}, status_code=415)
            # Only small JSON commands are accepted. No upload or user paths exist.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return JSONResponse({"detail": "Demande trop volumineuse."}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/api/status")
    def status():
        return {**manager.status(), "csrf_token": csrf_token}

    @app.post("/api/jobs", status_code=202)
    def create(request: JobRequest):
        return manager.create(request)

    @app.get("/api/jobs/{identifier}")
    def get(identifier: str):
        return manager.get(identifier)

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel(identifier: str):
        return manager.cancel(identifier)

    @app.get("/api/jobs/{identifier}/image")
    def image(identifier: str, download: bool = False):
        job = manager.get(identifier)
        if job["status"] != "completed":
            raise HTTPException(409, "Cette image n’est pas encore disponible.")
        path = settings.state / identifier / "image.png"
        try:
            read_png(path, job["width"], job["height"])
        except (OSError, RuntimeError):
            raise HTTPException(404, "Le fichier image n’est plus disponible.") from None
        return FileResponse(path, media_type="image/png", filename=f"chroma-{identifier[:8]}.png" if download else None)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static/index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=ROOT / "static", check_dir=False), name="static")
    return app


app = create_app()
