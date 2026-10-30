from typing import Any, Dict

import pytest

from auto_monitor.config import AppConfig, parse_config


def make_config(**overrides: Any) -> AppConfig:
    raw: Dict[str, Any] = {
        "ownerId": "test",
        "backends": {"uptimeKuma": {"enabled": True, "url": "http://kuma:3001"}},
    }
    for key, value in overrides.items():
        raw[key] = value
    return parse_config(raw)


def ingress(name: str, hosts, namespace: str = "default", tls_hosts=(), annotations=None,
            paths=("/",), ingress_class: str = "traefik") -> Dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": namespace, "annotations": annotations or {}},
        "spec": {
            "ingressClassName": ingress_class,
            "tls": [{"hosts": list(tls_hosts)}] if tls_hosts else [],
            "rules": [
                {"host": h, "http": {"paths": [{"path": p, "pathType": "Prefix"} for p in paths]}}
                for h in hosts
            ],
        },
    }


@pytest.fixture
def config() -> AppConfig:
    return make_config()
