"""Prometheus Operator ``Probe`` backend.

One Probe per target, scraped through a blackbox exporter, so uptime ends up
in Prometheus/Grafana alongside everything else. Probes are owned through
labels; only Probes carrying this instance's owner label are touched.
"""

import hashlib
import re
from typing import Any, Dict, List

from ..config import AppConfig
from ..discovery import Target
from ..log import logger
from . import Backend, SyncResult

GROUP, VERSION, PLURAL = "monitoring.coreos.com", "v1", "probes"
MANAGED_BY = "auto-monitor-k8s"


def probe_name(target: Target) -> str:
    """DNS-1123 name, unique per target key and stable across syncs."""
    slug = re.sub(r"[^a-z0-9]+", "-", target.url.split("://", 1)[-1].lower()).strip("-")[:40].strip("-")
    digest = hashlib.sha1(target.key.encode()).hexdigest()[:8]
    return f"am-{slug}-{digest}"


class BlackboxProbeBackend(Backend):
    name = "blackbox-probe"

    def __init__(self, config: AppConfig, custom_api: Any):
        self.config = config
        self.cfg = config.backends.blackbox_probe
        self.api = custom_api
        self.log = logger.bind(backend=self.name)
        prefix = config.annotation_prefix.rstrip("/")
        self.owner_label = f"{prefix}/owner"
        self.key_annotation = f"{prefix}/key"

    def _labels(self) -> Dict[str, str]:
        return {
            **self.cfg.labels,
            "app.kubernetes.io/managed-by": MANAGED_BY,
            self.owner_label: self.config.owner_id,
        }

    def desired_probe(self, target: Target) -> Dict[str, Any]:
        spec: Dict[str, Any] = {
            "jobName": self.cfg.job_name,
            "interval": f"{target.interval}s" if target.interval else self.cfg.interval,
            "module": self.cfg.module,
            "prober": {
                "url": self.cfg.prober_url,
                "scheme": self.cfg.prober_scheme,
                "path": self.cfg.prober_path,
            },
            "targets": {
                "staticConfig": {
                    "static": [target.url],
                    "labels": {
                        "monitor_name": target.name,
                        "source_namespace": target.namespace,
                        "source": target.source,
                    },
                },
            },
        }
        if self.cfg.scrape_timeout:
            spec["scrapeTimeout"] = self.cfg.scrape_timeout
        return {
            "apiVersion": f"{GROUP}/{VERSION}",
            "kind": "Probe",
            "metadata": {
                "name": probe_name(target),
                "namespace": self.cfg.namespace,
                "labels": self._labels(),
                "annotations": {self.key_annotation: target.key},
            },
            "spec": spec,
        }

    def sync(self, targets: List[Target]) -> SyncResult:
        ns, dry = self.cfg.namespace, self.config.dry_run
        result = SyncResult()
        existing_list = self.api.list_namespaced_custom_object(
            GROUP, VERSION, ns, PLURAL,
            label_selector=f"app.kubernetes.io/managed-by={MANAGED_BY},{self.owner_label}={self.config.owner_id}",
        ).get("items") or []
        existing = {p["metadata"]["name"]: p for p in existing_list}

        for target in targets:
            if not target.wants(self.name):
                continue
            desired = self.desired_probe(target)
            name = desired["metadata"]["name"]
            current = existing.pop(name, None)
            log = self.log.bind(probe=name, url=target.url)

            if current is None:
                if not dry:
                    self.api.create_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, desired)
                log.info("Would create probe" if dry else "Created probe")
                result.created += 1
                continue

            meta = current.get("metadata") or {}
            labels_ok = all((meta.get("labels") or {}).get(k) == v for k, v in desired["metadata"]["labels"].items())
            if current.get("spec") == desired["spec"] and labels_ok:
                result.unchanged += 1
                continue
            if not dry:
                # Replace, not merge-patch, so fields dropped from the config disappear
                desired["metadata"]["resourceVersion"] = meta.get("resourceVersion")
                self.api.replace_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, name, desired)
            log.info("Would update probe" if dry else "Updated probe")
            result.updated += 1

        if self.cfg.prune:
            for name in existing:
                if not dry:
                    self.api.delete_namespaced_custom_object(GROUP, VERSION, ns, PLURAL, name)
                self.log.info("Would delete probe" if dry else "Deleted probe, its resource is gone", probe=name)
                result.deleted += 1
        return result
