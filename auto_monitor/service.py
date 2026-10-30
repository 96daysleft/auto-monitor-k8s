"""Reconcile loop: discover targets, sync every backend, repeat."""

import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config

from .backends import Backend, build_backends
from .config import AppConfig
from .discovery import discover
from .log import logger


def load_kube_api() -> k8s_client.CustomObjectsApi:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()   # local runs: use the current kubectl context
    return k8s_client.CustomObjectsApi()


class MonitorService:
    def __init__(self, config: AppConfig, custom_api: Optional[Any] = None):
        self.config = config
        self.api = custom_api or load_kube_api()
        self.backends: List[Backend] = build_backends(config, self.api)
        self._stop = threading.Event()
        self.last_success: Dict[str, float] = {}     # backend name -> time of last good sync
        self.last_error: Dict[str, str] = {}
        self.started = time.time()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._stop.set())
        server = self._start_health_server() if self.config.health_port else None
        logger.info(
            "Starting",
            owner_id=self.config.owner_id,
            backends=[b.name for b in self.backends],
            interval=self.config.sync_interval_seconds,
            dry_run=self.config.dry_run,
        )
        try:
            while not self._stop.is_set():
                self.sync_once()
                self._stop.wait(self.config.sync_interval_seconds)
        finally:
            for backend in self.backends:
                backend.close()
            if server:
                server.shutdown()
            logger.info("Stopped")

    def sync_once(self) -> None:
        try:
            targets = discover(self.api, self.config)
        except Exception as e:  # noqa: BLE001 - keep looping, report via /healthz
            logger.error("Discovery failed, skipping this sync", error=str(e))
            for backend in self.backends:
                self.last_error[backend.name] = f"discovery: {e}"
            return

        logger.debug("Discovered targets", count=len(targets))
        for backend in self.backends:
            try:
                result = backend.sync(targets)
            except Exception as e:  # noqa: BLE001 - one broken backend mustn't block the others
                logger.error("Sync failed", backend=backend.name, error=str(e), exc_info=True)
                self.last_error[backend.name] = str(e)
                continue
            self.last_success[backend.name] = time.time()
            self.last_error.pop(backend.name, None)
            log = logger.info if (result.created or result.updated or result.deleted) else logger.debug
            log("Synced", backend=backend.name, targets=len(targets), **vars(result))

    def health(self) -> Dict[str, Any]:
        """Healthy while every backend has synced within the last few intervals."""
        grace = self.config.sync_interval_seconds * 5
        now = time.time()
        backends = {}
        healthy = True
        for b in self.backends:
            last = self.last_success.get(b.name)
            ok = (now - last if last else now - self.started) < grace
            healthy = healthy and ok
            backends[b.name] = {
                "ok": ok,
                "lastSuccess": last,
                "lastError": self.last_error.get(b.name),
            }
        return {"status": "ok" if healthy else "failing", "backends": backends}

    def _start_health_server(self) -> ThreadingHTTPServer:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self.path != "/healthz":
                    self.send_error(404)
                    return
                body = service.health()
                payload = json.dumps(body).encode()
                self.send_response(200 if body["status"] == "ok" else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("0.0.0.0", self.config.health_port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info("Health endpoint listening", port=self.config.health_port, path="/healthz")
        return server
