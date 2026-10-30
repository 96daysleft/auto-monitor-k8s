"""Monitoring backends: each one makes its tool match the list of targets."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List

from ..config import AppConfig
from ..discovery import Target


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deleted: int = 0        # deleted, or paused when pruning is off
    unchanged: int = 0


class Backend(ABC):
    name: str = ""

    @abstractmethod
    def sync(self, targets: List[Target]) -> SyncResult:
        """Create, update and remove this owner's monitors to match ``targets``."""

    def close(self) -> None:
        pass


def build_backends(config: AppConfig, custom_api: Any) -> List[Backend]:
    backends: List[Backend] = []
    if config.backends.uptime_kuma.enabled:
        from .uptime_kuma import UptimeKumaBackend
        backends.append(UptimeKumaBackend(config))
    if config.backends.blackbox_probe.enabled:
        from .blackbox_probe import BlackboxProbeBackend
        backends.append(BlackboxProbeBackend(config, custom_api))
    return backends
