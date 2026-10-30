"""Turn Kubernetes routing resources into monitor targets.

The parse functions take plain dicts (as returned by the Kubernetes API) so
they can be unit tested without a cluster; ``discover`` does the API calls.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import urlsplit

from .config import AppConfig
from .log import logger

BACKEND_NAMES = {"uptime-kuma", "blackbox-probe"}

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}
_TRAEFIK_HOST = re.compile(r"\bHost\(([^)]*)\)")
_TRAEFIK_PATH = re.compile(r"\bPath(?:Prefix)?\(\s*`([^`]+)`")
_BACKTICKED = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class Target:
    """One URL to monitor, plus per-resource overrides from annotations."""
    key: str                 # stable identity: <Kind>/<namespace>/<name>/<host>
    name: str                # display name in the monitoring tool
    url: str
    namespace: str
    source: str              # <Kind>/<namespace>/<name>, for descriptions and logs
    interval: Optional[int] = None
    accepted_status_codes: Optional[List[str]] = None
    ignore_tls: Optional[bool] = None
    backends: Optional[frozenset] = None   # None = every enabled backend

    def wants(self, backend: str) -> bool:
        return self.backends is None or backend in self.backends


@dataclass
class _Resource:
    """The parts of an Ingress / HTTPRoute / IngressRoute that matter here."""
    kind: str
    namespace: str
    name: str
    annotations: Dict[str, str]
    # host -> (scheme, paths in declaration order)
    hosts: Dict[str, "_HostInfo"] = field(default_factory=dict)


@dataclass
class _HostInfo:
    scheme: str
    paths: List[str] = field(default_factory=list)

    def add_path(self, path: Optional[str]) -> None:
        if path and path not in self.paths:
            self.paths.append(path)


def _parse_bool(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return None


def _split_list(value: Optional[str]) -> List[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _pick_path(paths: List[str]) -> str:
    """Monitor the site root when it is routed, otherwise the first routed path."""
    if not paths or "/" in paths:
        return "/"
    return paths[0]


# --- Resource parsers ----------------------------------------------------------

def _meta(obj: Dict[str, Any]) -> Dict[str, Any]:
    return obj.get("metadata") or {}


def parse_ingress(obj: Dict[str, Any], config: AppConfig) -> Optional[_Resource]:
    meta, spec = _meta(obj), obj.get("spec") or {}
    annotations = meta.get("annotations") or {}

    classes = config.discovery.ingress_classes
    if classes:
        cls = spec.get("ingressClassName") or annotations.get("kubernetes.io/ingress.class")
        if cls not in classes:
            return None

    tls_hosts: Set[str] = set()
    for tls in spec.get("tls") or []:
        tls_hosts.update(tls.get("hosts") or [])
    # Traefik can terminate TLS from an annotation without a spec.tls block
    traefik_tls = _parse_bool(annotations.get("traefik.ingress.kubernetes.io/router.tls")) is True

    res = _Resource("Ingress", meta.get("namespace", ""), meta.get("name", ""), annotations)
    for rule in spec.get("rules") or []:
        host = rule.get("host")
        if not host:
            continue
        scheme = "https" if host in tls_hosts or traefik_tls else config.discovery.default_scheme
        info = res.hosts.setdefault(host, _HostInfo(scheme))
        for p in (rule.get("http") or {}).get("paths") or []:
            info.add_path(p.get("path") or "/")
    return res


def parse_http_route(obj: Dict[str, Any], config: AppConfig) -> Optional[_Resource]:
    meta, spec = _meta(obj), obj.get("spec") or {}
    res = _Resource("HTTPRoute", meta.get("namespace", ""), meta.get("name", ""),
                    meta.get("annotations") or {})
    paths: List[str] = []
    for rule in spec.get("rules") or []:
        for match in rule.get("matches") or []:
            value = (match.get("path") or {}).get("value")
            if value and value not in paths:
                paths.append(value)
    for host in spec.get("hostnames") or []:
        res.hosts[host] = _HostInfo(config.discovery.http_route_scheme, list(paths))
    return res


def parse_traefik_ingress_route(obj: Dict[str, Any], config: AppConfig) -> Optional[_Resource]:
    meta, spec = _meta(obj), obj.get("spec") or {}
    tls = bool(spec.get("tls")) or "websecure" in (spec.get("entryPoints") or [])
    scheme = "https" if tls else config.discovery.default_scheme

    res = _Resource("IngressRoute", meta.get("namespace", ""), meta.get("name", ""),
                    meta.get("annotations") or {})
    for route in spec.get("routes") or []:
        match = route.get("match") or ""
        path_match = _TRAEFIK_PATH.search(match)
        for host_expr in _TRAEFIK_HOST.findall(match):
            for host in _BACKTICKED.findall(host_expr):
                info = res.hosts.setdefault(host, _HostInfo(scheme))
                info.add_path(path_match.group(1) if path_match else "/")
    return res


# --- Resource -> targets ---------------------------------------------------------

def targets_for(res: _Resource, config: AppConfig) -> List[Target]:
    """Apply filters and annotations to one resource."""
    disc = config.discovery
    prefix = config.annotation_prefix.rstrip("/") + "/"

    def ann(key: str) -> Optional[str]:
        return res.annotations.get(prefix + key)

    enabled = _parse_bool(ann("enabled"))
    if enabled is False or (disc.mode == "optIn" and enabled is not True):
        return []

    source = f"{res.kind}/{res.namespace}/{res.name}"
    log = logger.bind(source=source)

    interval = None
    if ann("interval"):
        try:
            interval = max(20, int(ann("interval")))
        except ValueError:
            log.warning("Ignoring invalid interval annotation", value=ann("interval"))
    codes = _split_list(ann("accepted-status-codes")) or None
    ignore_tls = _parse_bool(ann("ignore-tls"))
    backends = None
    if ann("backends"):
        wanted = set(_split_list(ann("backends")))
        unknown = wanted - BACKEND_NAMES
        if unknown:
            log.warning("Unknown backends in annotation", unknown=sorted(unknown))
        backends = frozenset(wanted & BACKEND_NAMES)

    only_hosts = set(_split_list(ann("hosts")))
    custom_name, custom_path, custom_url = ann("name"), ann("path"), ann("url")

    # host -> url
    urls: Dict[str, str] = {}
    if custom_url:
        parts = urlsplit(custom_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            log.warning("Ignoring invalid url annotation", value=custom_url)
            return []
        urls[parts.hostname] = custom_url   # explicit URL: skip host filters
    else:
        for host, info in res.hosts.items():
            if "*" in host or (only_hosts and host not in only_hosts):
                continue
            if disc.include_hosts and not any(re.fullmatch(p, host) for p in disc.include_hosts):
                continue
            if any(re.fullmatch(p, host) for p in disc.exclude_hosts):
                continue
            path = custom_path or _pick_path(info.paths)
            if not path.startswith("/"):
                path = "/" + path
            urls[host] = f"{info.scheme}://{host}{path}"

    targets = []
    for host, url in urls.items():
        if custom_name:
            name = custom_name if len(urls) == 1 else f"{custom_name} ({host})"
        else:
            path = urlsplit(url).path
            name = host if path in ("", "/") else f"{host}{path}"
        targets.append(Target(
            key=f"{source}/{host}",
            name=name,
            url=url,
            namespace=res.namespace,
            source=source,
            interval=interval,
            accepted_status_codes=codes,
            ignore_tls=ignore_tls,
            backends=backends,
        ))
    return targets


def build_targets(resources: Iterable[_Resource], config: AppConfig) -> List[Target]:
    """Targets for every resource, one per URL (the first resource by key wins)."""
    by_url: Dict[str, Target] = {}
    for target in sorted((t for r in resources for t in targets_for(r, config)), key=lambda t: t.key):
        existing = by_url.get(target.url)
        if existing:
            logger.debug("URL already monitored via another resource",
                         url=target.url, kept=existing.source, skipped=target.source)
            continue
        by_url[target.url] = target
    return sorted(by_url.values(), key=lambda t: t.key)


# --- Kubernetes API ----------------------------------------------------------------

# (config flag, group, version, plural, parser)
_SOURCES: List[tuple] = [
    ("ingress", "networking.k8s.io", "v1", "ingresses", parse_ingress),
    ("http_route", "gateway.networking.k8s.io", "v1", "httproutes", parse_http_route),
    ("traefik_ingress_route", "traefik.io", "v1alpha1", "ingressroutes", parse_traefik_ingress_route),
]


def discover(custom_api: Any, config: AppConfig) -> List[Target]:
    """List every enabled source kind and build the desired targets.

    Raises on any API error, so a partial listing never causes pruning.
    """
    disc = config.discovery
    resources: List[_Resource] = []
    for flag, group, version, plural, parser in _SOURCES:
        if not getattr(disc.sources, flag):
            continue
        items: List[Dict[str, Any]] = []
        if disc.namespaces:
            for ns in disc.namespaces:
                items += _list_all(lambda **kw: custom_api.list_namespaced_custom_object(
                    group, version, ns, plural, **kw))
        else:
            items = _list_all(lambda **kw: custom_api.list_cluster_custom_object(
                group, version, plural, **kw))
        for obj in items:
            if _meta(obj).get("namespace") in disc.exclude_namespaces:
                continue
            res = parser(obj, config)
            if res:
                resources.append(res)
    return build_targets(resources, config)


def _list_all(list_fn: Callable[..., Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    cont = None
    while True:
        kwargs = {"limit": 500}
        if cont:
            kwargs["_continue"] = cont
        page = list_fn(**kwargs)
        items += page.get("items") or []
        cont = (page.get("metadata") or {}).get("continue")
        if not cont:
            return items
