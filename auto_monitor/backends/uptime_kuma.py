"""Uptime Kuma backend, over its socket.io API (the same one the web UI uses).

Uptime Kuma has no REST API for managing monitors, so this logs in like a
browser would. Written against Uptime Kuma 2.x.

Managed monitors are recognised by a marker line in their description, which
carries this instance's ownerId and the target key. Monitors without the
marker (created by hand) are never touched.
"""

import re
import threading
from typing import Any, Dict, List, Optional, Set, Tuple

import socketio

from ..config import AppConfig
from ..discovery import Target
from ..log import logger
from . import Backend, SyncResult

_MARKER = re.compile(r"\[auto-monitor-k8s owner=(\S+) key=(\S+)\]")

# Fields compared on every sync; anything else on the monitor is left alone,
# so settings changed in the UI (other than these) survive.
MANAGED_FIELDS = (
    "name", "description", "type", "url", "method", "interval", "retryInterval",
    "maxretries", "timeout", "resendInterval", "accepted_statuscodes",
    "ignoreTls", "expiryNotification", "maxredirects",
)


class KumaError(Exception):
    pass


def marker(owner: str, key: str) -> str:
    return f"[auto-monitor-k8s owner={owner} key={key}]"


def parse_marker(description: Optional[str]) -> Optional[Tuple[str, str]]:
    m = _MARKER.search(description or "")
    return (m.group(1), m.group(2)) if m else None


def desired_monitor(target: Target, config: AppConfig) -> Dict[str, Any]:
    """The managed fields of the monitor for ``target`` (Uptime Kuma field names)."""
    d = config.backends.uptime_kuma.monitor
    interval = target.interval or d.interval
    monitor = {
        "type": "http",
        "name": target.name,
        "description": (
            f"Managed by auto-monitor-k8s from {target.source}; "
            "edits to its name, URL and check settings are overwritten.\n"
            + marker(config.owner_id, target.key)
        ),
        "url": target.url,
        "method": "GET",
        "interval": interval,
        "retryInterval": d.retry_interval,
        "maxretries": d.maxretries,
        # Uptime Kuma expects the timeout to be shorter than the interval
        "timeout": min(d.timeout, max(1, int(interval * 0.8))),
        "resendInterval": d.resend_interval,
        "accepted_statuscodes": target.accepted_status_codes or d.accepted_status_codes,
        "ignoreTls": d.ignore_tls if target.ignore_tls is None else target.ignore_tls,
        "expiryNotification": d.expiry_notification,
        "maxredirects": d.max_redirects,
    }
    monitor.update(d.extra)
    return monitor


def changed_fields(existing: Dict[str, Any], desired: Dict[str, Any]) -> List[str]:
    return [k for k, v in desired.items() if existing.get(k) != v]


def _notification_ids(id_map: Any) -> Set[int]:
    """notificationIDList is {"<id>": true, ...}; return the enabled ids."""
    if not isinstance(id_map, dict):
        return set()
    return {int(k) for k, v in id_map.items() if v}


class KumaClient:
    """Thin synchronous wrapper around Uptime Kuma's socket.io events."""

    def __init__(self, url: str, username: str, password: str, timeout: int):
        self.url, self.username, self.password, self.timeout = url, username, password, timeout
        self.monitors: Dict[int, Dict[str, Any]] = {}
        self.notifications: List[Dict[str, Any]] = []
        self._info_received = threading.Event()
        self._monitor_list_received = threading.Event()
        self._sio = socketio.Client(reconnection=False)
        self._sio.on("info", lambda *_: self._info_received.set())
        self._sio.on("monitorList", self._on_monitor_list)
        self._sio.on("updateMonitorIntoList", self._on_update_monitors)
        self._sio.on("deleteMonitorFromList", self._on_delete_monitor)
        self._sio.on("notificationList", self._on_notification_list)

    # -- server pushes --
    def _on_monitor_list(self, data: Dict[str, Any]) -> None:
        self.monitors = {int(k): v for k, v in (data or {}).items()}
        self._monitor_list_received.set()

    def _on_update_monitors(self, data: Dict[str, Any]) -> None:
        for k, v in (data or {}).items():
            self.monitors[int(k)] = v

    def _on_delete_monitor(self, monitor_id: Any) -> None:
        self.monitors.pop(int(monitor_id), None)

    def _on_notification_list(self, data: List[Dict[str, Any]]) -> None:
        self.notifications = list(data or [])

    # -- connection --
    @property
    def connected(self) -> bool:
        return self._sio.connected

    def connect(self) -> None:
        self._monitor_list_received.clear()
        self._sio.connect(self.url, transports=["websocket"], wait_timeout=self.timeout)
        # The server only registers its event handlers after sending "info";
        # anything emitted before that is silently dropped.
        if not self._info_received.wait(self.timeout):
            raise KumaError("timed out waiting for the server info event (is this Uptime Kuma?)")
        if self.username:
            res = self._call("login", {"username": self.username, "password": self.password, "token": ""})
            if res.get("tokenRequired"):
                raise KumaError("login requires a 2FA token: use an account without 2FA")
        # With auth disabled the lists are pushed right after connecting
        if not self._monitor_list_received.wait(self.timeout):
            raise KumaError("timed out waiting for the monitor list (wrong credentials?)")

    def disconnect(self) -> None:
        try:
            self._sio.disconnect()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def _call(self, event: str, *args: Any) -> Dict[str, Any]:
        data = args[0] if len(args) == 1 else args
        res = self._sio.call(event, data, timeout=self.timeout)
        if not isinstance(res, dict) or not res.get("ok"):
            msg = res.get("msg") if isinstance(res, dict) else res
            raise KumaError(f"{event} failed: {msg}")
        return res

    # -- operations --
    def add_monitor(self, monitor: Dict[str, Any]) -> int:
        return int(self._call("add", monitor)["monitorID"])

    def edit_monitor(self, monitor: Dict[str, Any]) -> None:
        self._call("editMonitor", monitor)

    def delete_monitor(self, monitor_id: int) -> None:
        self._call("deleteMonitor", monitor_id, False)   # False: don't delete group children

    def pause_monitor(self, monitor_id: int) -> None:
        self._call("pauseMonitor", monitor_id)

    def get_tags(self) -> List[Dict[str, Any]]:
        return self._call("getTags").get("tags") or []

    def add_tag(self, name: str, color: str) -> Dict[str, Any]:
        return self._call("addTag", {"name": name, "color": color})["tag"]

    def add_monitor_tag(self, tag_id: int, monitor_id: int, value: str) -> None:
        self._call("addMonitorTag", tag_id, monitor_id, value)

    def delete_monitor_tag(self, tag_id: int, monitor_id: int, value: str) -> None:
        self._call("deleteMonitorTag", tag_id, monitor_id, value)


class UptimeKumaBackend(Backend):
    name = "uptime-kuma"

    def __init__(self, config: AppConfig):
        self.config = config
        self.cfg = config.backends.uptime_kuma
        self.client: Optional[KumaClient] = None
        self.log = logger.bind(backend=self.name)

    def _client(self) -> KumaClient:
        if self.client is None or not self.client.connected:
            if self.client:
                self.client.disconnect()
            client = KumaClient(self.cfg.url, self.cfg.username, self.cfg.password,
                                self.cfg.connect_timeout_seconds)
            client.connect()
            self.log.info("Connected to Uptime Kuma", url=self.cfg.url, monitors=len(client.monitors))
            self.client = client
        return self.client

    def close(self) -> None:
        if self.client:
            self.client.disconnect()
            self.client = None

    def _wanted_notifications(self, client: KumaClient) -> Dict[str, bool]:
        ids: Set[int] = set()
        by_name = {n.get("name"): n for n in client.notifications}
        for name in self.cfg.notifications:
            if name in by_name:
                ids.add(int(by_name[name]["id"]))
            else:
                self.log.warning("Notification not found in Uptime Kuma", notification=name)
        if self.cfg.use_default_notifications:
            ids |= {int(n["id"]) for n in client.notifications if n.get("isDefault")}
        return {str(i): True for i in sorted(ids)}

    def _tag_id(self, client: KumaClient) -> Optional[int]:
        tag = self.cfg.tag
        if not tag.enabled:
            return None
        for t in client.get_tags():
            if t.get("name") == tag.name:
                return int(t["id"])
        if self.config.dry_run:
            self.log.info("Would create tag", tag=tag.name)
            return None
        created = client.add_tag(tag.name, tag.color)
        self.log.info("Created tag", tag=tag.name)
        return int(created["id"])

    def _sync_tag(self, client: KumaClient, tag_id: Optional[int], monitor_id: int,
                  existing_tags: List[Dict[str, Any]], namespace: str) -> None:
        if tag_id is None or self.config.dry_run:
            return
        values = {t.get("value") or "" for t in existing_tags if int(t.get("tag_id", -1)) == tag_id}
        if namespace not in values:
            client.add_monitor_tag(tag_id, monitor_id, namespace)
        for stale in values - {namespace}:
            client.delete_monitor_tag(tag_id, monitor_id, stale)

    def sync(self, targets: List[Target]) -> SyncResult:
        try:
            return self._sync(targets)
        except Exception:
            # Drop the connection so the next sync starts from a fresh login
            self.close()
            raise

    def _sync(self, targets: List[Target]) -> SyncResult:
        client = self._client()
        dry = self.config.dry_run
        result = SyncResult()
        notifications = self._wanted_notifications(client)
        tag_id = self._tag_id(client)

        # key -> monitor for monitors this owner manages
        managed: Dict[str, Dict[str, Any]] = {}
        for monitor in list(client.monitors.values()):
            owner_key = parse_marker(monitor.get("description"))
            if not owner_key or owner_key[0] != self.config.owner_id:
                continue
            key = owner_key[1]
            if key in managed:
                # Leftover from a crash between add and the next list refresh
                self.log.warning("Duplicate managed monitor, removing", key=key, monitor_id=monitor["id"])
                if not dry:
                    client.delete_monitor(int(monitor["id"]))
                continue
            managed[key] = monitor

        for target in targets:
            if not target.wants(self.name):
                continue
            desired = desired_monitor(target, self.config)
            existing = managed.pop(target.key, None)
            log = self.log.bind(monitor=target.name, url=target.url)

            if existing is None:
                if dry:
                    log.info("Would create monitor")
                else:
                    payload = {
                        **desired,
                        "notificationIDList": notifications,
                        "active": True,
                        "parent": None,
                        "upsideDown": False,
                        "conditions": [],
                        "kafkaProducerBrokers": [],
                        "kafkaProducerSaslOptions": {"mechanism": "None"},
                        "rabbitmqNodes": [],
                    }
                    monitor_id = client.add_monitor(payload)
                    self._sync_tag(client, tag_id, monitor_id, [], target.namespace)
                    log.info("Created monitor", monitor_id=monitor_id)
                result.created += 1
                continue

            monitor_id = int(existing["id"])
            changes = changed_fields(existing, desired)
            if _notification_ids(existing.get("notificationIDList")) != _notification_ids(notifications):
                changes.append("notificationIDList")
            if changes:
                if dry:
                    log.info("Would update monitor", monitor_id=monitor_id, fields=changes)
                else:
                    # editMonitor overwrites every field, so start from the current monitor
                    client.edit_monitor({**existing, **desired, "notificationIDList": notifications})
                    log.info("Updated monitor", monitor_id=monitor_id, fields=changes)
                result.updated += 1
            else:
                result.unchanged += 1
            self._sync_tag(client, tag_id, monitor_id, existing.get("tags") or [], target.namespace)

        for key, monitor in managed.items():
            monitor_id = int(monitor["id"])
            log = self.log.bind(monitor=monitor.get("name"), monitor_id=monitor_id, key=key)
            if self.cfg.prune:
                if dry:
                    log.info("Would delete monitor")
                else:
                    client.delete_monitor(monitor_id)
                    log.info("Deleted monitor, its resource is gone")
                result.deleted += 1
            elif monitor.get("active"):
                if dry:
                    log.info("Would pause monitor")
                else:
                    client.pause_monitor(monitor_id)
                    log.info("Paused monitor, its resource is gone")
                result.deleted += 1
        return result
