from typing import Any, Dict, List

from auto_monitor.backends.uptime_kuma import (
    UptimeKumaBackend,
    desired_monitor,
    marker,
    parse_marker,
)
from auto_monitor.discovery import Target

from .conftest import make_config


def target(host: str, ns: str = "default", **kw: Any) -> Target:
    return Target(key=f"Ingress/{ns}/app/{host}", name=host, url=f"https://{host}/",
                  namespace=ns, source=f"Ingress/{ns}/app", **kw)


class FakeKuma:
    connected = True

    def __init__(self, monitors: List[Dict[str, Any]] = (), notifications=(), tags=()):
        self.monitors = {m["id"]: m for m in monitors}
        self.notifications = list(notifications)
        self.tags = list(tags)
        self.calls: List[tuple] = []
        self._next_id = 100

    def disconnect(self):
        pass

    def add_monitor(self, monitor):
        self._next_id += 1
        self.calls.append(("add", monitor))
        return self._next_id

    def edit_monitor(self, monitor):
        self.calls.append(("edit", monitor))

    def delete_monitor(self, monitor_id):
        self.calls.append(("delete", monitor_id))

    def pause_monitor(self, monitor_id):
        self.calls.append(("pause", monitor_id))

    def get_tags(self):
        return self.tags

    def add_tag(self, name, color):
        self.calls.append(("addTag", name))
        tag = {"id": 7, "name": name, "color": color}
        self.tags.append(tag)
        return tag

    def add_monitor_tag(self, tag_id, monitor_id, value):
        self.calls.append(("addMonitorTag", tag_id, monitor_id, value))

    def delete_monitor_tag(self, tag_id, monitor_id, value):
        self.calls.append(("deleteMonitorTag", tag_id, monitor_id, value))


def backend(fake: FakeKuma, **config_overrides) -> UptimeKumaBackend:
    config = make_config(**config_overrides)
    b = UptimeKumaBackend(config)
    b.client = fake
    return b


def existing_for(t: Target, config, monitor_id: int, **fields) -> Dict[str, Any]:
    return {"id": monitor_id, **desired_monitor(t, config), "notificationIDList": {},
            "active": True, "tags": [{"tag_id": 7, "value": t.namespace}], **fields}


def test_marker_round_trip():
    assert parse_marker("hello\n" + marker("prod", "Ingress/a/b/c.example.com")) == (
        "prod", "Ingress/a/b/c.example.com")
    assert parse_marker("hand made monitor") is None
    assert parse_marker(None) is None


def test_creates_missing_monitor_with_tag_and_default_notifications():
    fake = FakeKuma(notifications=[{"id": 1, "name": "Discord", "isDefault": True},
                                   {"id": 2, "name": "Email", "isDefault": False}])
    result = backend(fake).sync([target("a.example.com", ns="apps")])
    assert result.created == 1
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["addTag", "add", "addMonitorTag"]
    added = fake.calls[1][1]
    assert added["url"] == "https://a.example.com/"
    assert added["type"] == "http"
    assert added["notificationIDList"] == {"1": True}
    assert parse_marker(added["description"]) == ("test", "Ingress/apps/app/a.example.com")
    assert fake.calls[2] == ("addMonitorTag", 7, 101, "apps")


def test_named_notifications():
    fake = FakeKuma(notifications=[{"id": 1, "name": "Discord", "isDefault": False},
                                   {"id": 2, "name": "Email", "isDefault": False}])
    b = backend(fake, backends={"uptimeKuma": {
        "enabled": True, "url": "http://kuma", "notifications": ["Email", "Missing"]}})
    b.sync([target("a.example.com")])
    assert fake.calls[-2][1]["notificationIDList"] == {"2": True}


def test_unchanged_monitor_is_left_alone():
    config = make_config()
    t = target("a.example.com")
    fake = FakeKuma(monitors=[existing_for(t, config, 5)], tags=[{"id": 7, "name": "k8s"}])
    result = backend(fake).sync([t])
    assert result.unchanged == 1
    assert fake.calls == []


def test_changed_monitor_is_edited_keeping_other_fields():
    config = make_config()
    t = target("a.example.com")
    current = existing_for(t, config, 5, url="https://old.example.com/", keyword="keep-me")
    fake = FakeKuma(monitors=[current], tags=[{"id": 7, "name": "k8s"}])
    result = backend(fake).sync([t])
    assert result.updated == 1
    [(kind, edited)] = fake.calls
    assert kind == "edit"
    assert edited["id"] == 5
    assert edited["url"] == "https://a.example.com/"
    assert edited["keyword"] == "keep-me"


def test_tag_value_follows_namespace():
    config = make_config()
    t = target("a.example.com", ns="new")
    current = existing_for(t, config, 5, tags=[{"tag_id": 7, "value": "old"}])
    fake = FakeKuma(monitors=[current], tags=[{"id": 7, "name": "k8s"}])
    backend(fake).sync([t])
    assert fake.calls == [("addMonitorTag", 7, 5, "new"), ("deleteMonitorTag", 7, 5, "old")]


def test_orphans_deleted_but_foreign_monitors_untouched():
    config = make_config()
    gone = existing_for(target("gone.example.com"), config, 5)
    other_owner = {"id": 6, "name": "x", "description": marker("someone-else", "Ingress/a/b/c")}
    by_hand = {"id": 8, "name": "manual", "description": "my monitor"}
    fake = FakeKuma(monitors=[gone, other_owner, by_hand], tags=[{"id": 7, "name": "k8s"}])
    result = backend(fake).sync([])
    assert result.deleted == 1
    assert fake.calls == [("delete", 5)]


def test_orphans_paused_when_prune_disabled():
    config = make_config()
    gone = existing_for(target("gone.example.com"), config, 5)
    fake = FakeKuma(monitors=[gone], tags=[{"id": 7, "name": "k8s"}])
    backend(fake, backends={"uptimeKuma": {"enabled": True, "url": "http://kuma", "prune": False}}).sync([])
    assert fake.calls == [("pause", 5)]


def test_target_opted_out_of_backend_is_removed():
    config = make_config()
    t = target("a.example.com", backends=frozenset({"blackbox-probe"}))
    fake = FakeKuma(monitors=[existing_for(t, config, 5)], tags=[{"id": 7, "name": "k8s"}])
    backend(fake).sync([t])
    assert fake.calls == [("delete", 5)]


def test_dry_run_changes_nothing():
    config = make_config()
    gone = existing_for(target("gone.example.com"), config, 5)
    fake = FakeKuma(monitors=[gone])
    result = backend(fake, dryRun=True).sync([target("new.example.com")])
    assert (result.created, result.deleted) == (1, 1)
    assert fake.calls == []


def test_overrides_and_timeout_clamp():
    config = make_config(backends={"uptimeKuma": {
        "enabled": True, "url": "http://kuma",
        "monitor": {"timeout": 48, "extra": {"method": "HEAD"}}}})
    m = desired_monitor(target("a.example.com", interval=30, ignore_tls=True,
                               accepted_status_codes=["200-399"]), config)
    assert m["interval"] == 30
    assert m["timeout"] == 24
    assert m["ignoreTls"] is True
    assert m["accepted_statuscodes"] == ["200-399"]
    assert m["method"] == "HEAD"
