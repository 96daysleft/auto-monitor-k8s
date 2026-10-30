from auto_monitor.discovery import (
    build_targets,
    discover,
    parse_http_route,
    parse_ingress,
    parse_traefik_ingress_route,
)

from .conftest import ingress, make_config

P = "auto-monitor.96daysleft.com/"


def targets(objs, config, parser=parse_ingress):
    return build_targets([parser(o, config) for o in objs if parser(o, config)], config)


def test_ingress_tls_host_is_https(config):
    [t] = targets([ingress("app", ["app.example.com"], tls_hosts=["app.example.com"])], config)
    assert t.url == "https://app.example.com/"
    assert t.name == "app.example.com"
    assert t.key == "Ingress/default/app/app.example.com"
    assert t.namespace == "default"


def test_ingress_without_tls_uses_default_scheme(config):
    [t] = targets([ingress("app", ["app.example.com"])], config)
    assert t.url == "http://app.example.com/"


def test_traefik_tls_annotation_is_https(config):
    obj = ingress("app", ["app.example.com"], annotations={"traefik.ingress.kubernetes.io/router.tls": "true"})
    [t] = targets([obj], config)
    assert t.url.startswith("https://")


def test_subpath_only_ingress_monitors_that_path(config):
    [t] = targets([ingress("app", ["a.example.com"], paths=("/api", "/ui"))], config)
    assert t.url == "http://a.example.com/api"
    assert t.name == "a.example.com/api"


def test_wildcard_and_hostless_rules_skipped(config):
    obj = ingress("app", ["*.example.com", "ok.example.com"])
    obj["spec"]["rules"].append({"http": {"paths": [{"path": "/"}]}})
    assert [t.url for t in targets([obj], config)] == ["http://ok.example.com/"]


def test_opt_out_annotation(config):
    obj = ingress("app", ["a.example.com"], annotations={P + "enabled": "false"})
    assert targets([obj], config) == []


def test_opt_in_mode():
    config = make_config(discovery={"mode": "optIn"})
    plain = ingress("plain", ["a.example.com"])
    opted = ingress("opted", ["b.example.com"], annotations={P + "enabled": "true"})
    assert [t.url for t in targets([plain, opted], config)] == ["http://b.example.com/"]


def test_host_filters():
    config = make_config(discovery={
        "includeHosts": [r".*\.example\.com"],
        "excludeHosts": [r"internal\..*"],
    })
    obj = ingress("app", ["a.example.com", "internal.example.com", "other.org"])
    assert [t.url for t in targets([obj], config)] == ["http://a.example.com/"]


def test_ingress_class_filter():
    config = make_config(discovery={"ingressClasses": ["traefik-external"]})
    assert targets([ingress("a", ["a.example.com"])], config) == []
    assert len(targets([ingress("b", ["b.example.com"], ingress_class="traefik-external")], config)) == 1


def test_annotation_overrides(config):
    obj = ingress("app", ["a.example.com"], annotations={
        P + "name": "My App",
        P + "path": "health",
        P + "interval": "120",
        P + "accepted-status-codes": "200-299, 401",
        P + "ignore-tls": "true",
        P + "backends": "uptime-kuma, bogus",
    })
    [t] = targets([obj], config)
    assert t.name == "My App"
    assert t.url == "http://a.example.com/health"
    assert t.interval == 120
    assert t.accepted_status_codes == ["200-299", "401"]
    assert t.ignore_tls is True
    assert t.wants("uptime-kuma") and not t.wants("blackbox-probe")


def test_custom_name_with_several_hosts(config):
    obj = ingress("app", ["a.example.com", "b.example.com"], annotations={P + "name": "App"})
    assert [t.name for t in targets([obj], config)] == ["App (a.example.com)", "App (b.example.com)"]


def test_hosts_annotation_limits_hosts(config):
    obj = ingress("app", ["a.example.com", "b.example.com"], annotations={P + "hosts": "b.example.com"})
    assert [t.url for t in targets([obj], config)] == ["http://b.example.com/"]


def test_url_annotation(config):
    obj = ingress("app", ["a.example.com"], annotations={P + "url": "https://status.example.com/ping"})
    [t] = targets([obj], config)
    assert t.url == "https://status.example.com/ping"
    assert t.key == "Ingress/default/app/status.example.com"


def test_duplicate_urls_collapse(config):
    a = ingress("a", ["same.example.com"], namespace="ns1")
    b = ingress("b", ["same.example.com"], namespace="ns2")
    [t] = targets([b, a], config)
    assert t.source == "Ingress/ns1/a"


def test_http_route(config):
    obj = {
        "metadata": {"name": "web", "namespace": "apps"},
        "spec": {
            "hostnames": ["web.example.com"],
            "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": "/"}}]}],
        },
    }
    [t] = targets([obj], config, parse_http_route)
    assert t.url == "https://web.example.com/"
    assert t.key == "HTTPRoute/apps/web/web.example.com"


def test_traefik_ingress_route(config):
    obj = {
        "metadata": {"name": "web", "namespace": "apps"},
        "spec": {
            "entryPoints": ["websecure"],
            "routes": [
                {"match": "Host(`a.example.com`) || Host(`b.example.com`)"},
                {"match": "Host(`c.example.com`) && PathPrefix(`/api`)"},
            ],
        },
    }
    urls = [t.url for t in targets([obj], config, parse_traefik_ingress_route)]
    assert urls == ["https://a.example.com/", "https://b.example.com/", "https://c.example.com/api"]


class FakeCustomApi:
    def __init__(self, items_by_plural):
        self.items = items_by_plural
        self.calls = []

    def list_cluster_custom_object(self, group, version, plural, **kw):
        self.calls.append(("cluster", plural))
        items = self.items.get(plural, [])
        if "_continue" not in kw and len(items) > 1:
            return {"items": items[:1], "metadata": {"continue": "next"}}
        return {"items": items[1:] if "_continue" in kw else items, "metadata": {}}

    def list_namespaced_custom_object(self, group, version, ns, plural, **kw):
        self.calls.append((ns, plural))
        return {"items": [i for i in self.items.get(plural, []) if i["metadata"]["namespace"] == ns]}


def test_discover_paginates_and_excludes_namespaces():
    config = make_config(discovery={"excludeNamespaces": ["kube-system"]})
    api = FakeCustomApi({"ingresses": [
        ingress("a", ["a.example.com"]),
        ingress("dash", ["dash.example.com"], namespace="kube-system"),
        ingress("b", ["b.example.com"]),
    ]})
    assert [t.url for t in discover(api, config)] == ["http://a.example.com/", "http://b.example.com/"]
    assert api.calls == [("cluster", "ingresses"), ("cluster", "ingresses")]


def test_discover_namespaced():
    config = make_config(discovery={"namespaces": ["apps"], "sources": {"ingress": True, "httpRoute": True}})
    api = FakeCustomApi({"ingresses": [ingress("a", ["a.example.com"], namespace="apps"),
                                        ingress("b", ["b.example.com"])]})
    assert [t.url for t in discover(api, config)] == ["http://a.example.com/"]
    assert api.calls == [("apps", "ingresses"), ("apps", "httproutes")]
