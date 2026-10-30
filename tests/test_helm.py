"""The config.yaml the Helm chart renders must be accepted by the app."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from auto_monitor.config import load_config

ROOT = Path(__file__).parent.parent
CHART = ROOT / "helm" / "auto-monitor-k8s"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def render(*args: str) -> list:
    out = subprocess.run(
        ["helm", "template", "test", str(CHART), "-n", "auto-monitor", *args],
        check=True, capture_output=True, text=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def load_rendered(docs, tmp_path):
    secret = next(d for d in docs if d["kind"] == "Secret")
    path = tmp_path / "config.yaml"
    path.write_text(secret["stringData"]["config.yaml"])
    return load_config(str(path))


@pytest.fixture(autouse=True)
def kuma_env(monkeypatch):
    monkeypatch.setenv("UPTIME_KUMA_USERNAME", "admin")
    monkeypatch.setenv("UPTIME_KUMA_PASSWORD", "secret")


def test_default_values_load(tmp_path):
    docs = render()
    config = load_rendered(docs, tmp_path)
    assert config.backends.uptime_kuma.enabled
    assert config.backends.uptime_kuma.password == "secret"
    assert not config.backends.blackbox_probe.enabled
    assert {d["kind"] for d in docs} == {
        "Secret", "ServiceAccount", "ClusterRole", "ClusterRoleBinding", "Deployment"}


def test_example_values_load(tmp_path):
    docs = render("-f", str(ROOT / "examples" / "values-talos.yaml"))
    config = load_rendered(docs, tmp_path)
    assert config.owner_id == "talos"
    assert config.discovery.sources.traefik_ingress_route
    role = next(d for d in docs if d["kind"] == "ClusterRole")
    assert {r["apiGroups"][0] for r in role["rules"]} == {"networking.k8s.io", "traefik.io"}


def test_blackbox_probe_defaults_to_release_namespace(tmp_path):
    docs = render("--set", "backends.blackboxProbe.enabled=true",
                  "--set", "backends.blackboxProbe.proberUrl=bb:9115")
    config = load_rendered(docs, tmp_path)
    assert config.backends.blackbox_probe.namespace == "auto-monitor"
    role = next(d for d in docs if d["kind"] == "Role")
    assert role["metadata"]["namespace"] == "auto-monitor"


def test_existing_secret_skips_rendering():
    docs = render("--set", "existingSecret=my-config")
    assert "Secret" not in [d["kind"] for d in docs]
    deploy = next(d for d in docs if d["kind"] == "Deployment")
    assert deploy["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] == "my-config"


def test_no_backend_fails():
    with pytest.raises(subprocess.CalledProcessError) as e:
        render("--set", "backends.uptimeKuma.enabled=false")
    assert "enable backends" in e.value.stderr
