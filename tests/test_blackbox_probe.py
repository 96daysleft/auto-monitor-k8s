from auto_monitor.backends.blackbox_probe import BlackboxProbeBackend, probe_name
from auto_monitor.discovery import Target

from .conftest import make_config


def target(host: str) -> Target:
    return Target(key=f"Ingress/apps/app/{host}", name=host, url=f"https://{host}/",
                  namespace="apps", source="Ingress/apps/app")


class FakeApi:
    def __init__(self, probes=()):
        self.probes = {p["metadata"]["name"]: p for p in probes}
        self.calls = []

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=""):
        self.calls.append(("list", label_selector))
        return {"items": list(self.probes.values())}

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        self.calls.append(("create", body["metadata"]["name"]))

    def replace_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self.calls.append(("replace", name, body["metadata"]["resourceVersion"]))

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        self.calls.append(("delete", name))


def backend(api):
    config = make_config(backends={"blackboxProbe": {
        "enabled": True, "namespace": "monitoring", "proberUrl": "blackbox:9115",
        "labels": {"release": "prometheus"}}})
    return BlackboxProbeBackend(config, api)


def test_probe_name_is_dns_safe_and_stable():
    name = probe_name(target("A.Very.Long.Hostname.example.com"))
    assert name == probe_name(target("A.Very.Long.Hostname.example.com"))
    assert name.startswith("am-a-very-long-hostname-example-com-")
    assert len(name) <= 63


def test_create_update_delete():
    t_new, t_changed = target("new.example.com"), target("changed.example.com")
    api = FakeApi()
    b = backend(api)
    changed = b.desired_probe(t_changed)
    changed["metadata"]["resourceVersion"] = "42"
    changed["spec"]["module"] = "old_module"
    same = b.desired_probe(target("same.example.com"))
    stale = {"metadata": {"name": "am-stale-1234", "labels": {}}, "spec": {}}
    api.probes = {p["metadata"]["name"]: p for p in (changed, same, stale)}

    result = b.sync([t_new, t_changed, target("same.example.com")])
    assert (result.created, result.updated, result.unchanged, result.deleted) == (1, 1, 1, 1)
    assert api.calls[0] == ("list", "app.kubernetes.io/managed-by=auto-monitor-k8s,"
                                    "auto-monitor.96daysleft.com/owner=test")
    assert ("create", probe_name(t_new)) in api.calls
    assert ("replace", probe_name(t_changed), "42") in api.calls
    assert ("delete", "am-stale-1234") in api.calls


def test_probe_spec():
    probe = backend(FakeApi()).desired_probe(target("a.example.com"))
    assert probe["metadata"]["labels"]["release"] == "prometheus"
    assert probe["spec"]["prober"] == {"url": "blackbox:9115", "scheme": "http", "path": "/probe"}
    assert probe["spec"]["targets"]["staticConfig"]["static"] == ["https://a.example.com/"]
    assert "scrapeTimeout" not in probe["spec"]
