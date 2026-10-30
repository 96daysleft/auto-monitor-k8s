"""Application configuration, loaded from a YAML file.

Keys may be written in camelCase (as rendered by the Helm chart) or
snake_case. Any string value may reference environment variables with
``${VAR}`` or ``${VAR:-default}`` so secrets can be injected at runtime
(e.g. from a Kubernetes Secret or OpenBao) instead of living in the file.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

DEFAULT_CONFIG_PATH = "config.yaml"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


class ConfigModel(BaseModel):
    """Base for all config models: camelCase or snake_case keys, unknown keys rejected."""
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class SourcesConfig(ConfigModel):
    """Which Kubernetes resource kinds are scanned for hostnames."""
    ingress: bool = True                   # networking.k8s.io/v1 Ingress
    http_route: bool = False               # gateway.networking.k8s.io/v1 HTTPRoute
    traefik_ingress_route: bool = False    # traefik.io/v1alpha1 IngressRoute


class DiscoveryConfig(ConfigModel):
    # optOut: every discovered host is monitored unless annotated enabled: "false"
    # optIn:  only resources annotated enabled: "true" are monitored
    mode: Literal["optIn", "optOut"] = "optOut"
    namespaces: List[str] = Field(default_factory=list)          # empty = all namespaces
    exclude_namespaces: List[str] = Field(default_factory=list)
    ingress_classes: List[str] = Field(default_factory=list)     # empty = any class
    # Regexes matched (fullmatch) against the hostname. Use these to keep
    # internal-only hosts out, e.g. excludeHosts: ['.*\.internal\.example\.com']
    include_hosts: List[str] = Field(default_factory=list)       # empty = all hosts
    exclude_hosts: List[str] = Field(default_factory=list)
    # Scheme for hosts with no TLS config. Hosts listed under an Ingress's
    # spec.tls (or a TLS IngressRoute) are always https.
    default_scheme: Literal["http", "https"] = "http"
    # Gateway API listeners decide TLS, which a route can't see: assume this.
    http_route_scheme: Literal["http", "https"] = "https"
    sources: SourcesConfig = Field(default_factory=SourcesConfig)

    @field_validator("include_hosts", "exclude_hosts")
    @classmethod
    def _valid_regexes(cls, v: List[str]) -> List[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"invalid host regex '{pattern}': {e}") from e
        return v


class KumaTagConfig(ConfigModel):
    """Tag added to every managed monitor; its value is the source namespace."""
    enabled: bool = True
    name: str = "k8s"
    color: str = "#2563EB"


class KumaMonitorDefaults(ConfigModel):
    """Monitor settings; interval, accepted codes and ignoreTls can be overridden per resource."""
    interval: int = Field(default=60, ge=20)
    retry_interval: int = Field(default=60, ge=20)
    maxretries: int = Field(default=2, ge=0)
    timeout: int = Field(default=48, ge=1)            # seconds; Kuma's default is 0.8 * interval
    resend_interval: int = Field(default=0, ge=0)     # re-notify every N failed beats, 0 = never
    accepted_status_codes: List[str] = Field(default_factory=lambda: ["200-299"])
    ignore_tls: bool = False
    expiry_notification: bool = True                  # alert before the TLS cert expires
    max_redirects: int = Field(default=10, ge=0)
    # Raw Uptime Kuma monitor fields, applied last (e.g. {"method": "HEAD"})
    extra: Dict[str, Any] = Field(default_factory=dict)


class UptimeKumaConfig(ConfigModel):
    enabled: bool = False
    url: str = ""                       # e.g. http://uptime-kuma.uptime-kuma.svc:3001
    # Leave username empty only if Uptime Kuma has authentication disabled
    username: str = ""
    password: str = ""
    # Delete managed monitors whose resource no longer exists. When false
    # they are paused instead, so history is kept.
    prune: bool = True
    tag: KumaTagConfig = Field(default_factory=KumaTagConfig)
    # Notification names (as shown in Uptime Kuma) attached to every monitor
    notifications: List[str] = Field(default_factory=list)
    # Also attach notifications marked "Default enabled" in Uptime Kuma
    use_default_notifications: bool = True
    monitor: KumaMonitorDefaults = Field(default_factory=KumaMonitorDefaults)
    connect_timeout_seconds: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def _check(self) -> "UptimeKumaConfig":
        if self.enabled and not self.url:
            raise ValueError("backends.uptimeKuma.url is required when the backend is enabled")
        if self.username and not self.password:
            raise ValueError("backends.uptimeKuma.password is required when username is set")
        return self


class BlackboxProbeConfig(ConfigModel):
    """Prometheus Operator Probe objects scraped through a blackbox exporter."""
    enabled: bool = False
    namespace: str = ""                 # where Probe objects are created
    prober_url: str = ""                # host:port of the blackbox exporter
    prober_scheme: Literal["http", "https"] = "http"
    prober_path: str = "/probe"
    module: str = "http_2xx"
    interval: str = "60s"
    scrape_timeout: str = ""            # empty = Prometheus default
    job_name: str = "auto-monitor-k8s"
    # Extra labels on each Probe, e.g. what your Prometheus probeSelector matches
    labels: Dict[str, str] = Field(default_factory=dict)
    prune: bool = True

    @model_validator(mode="after")
    def _check(self) -> "BlackboxProbeConfig":
        if self.enabled and not (self.namespace and self.prober_url):
            raise ValueError(
                "backends.blackboxProbe.namespace and proberUrl are required when the backend is enabled"
            )
        return self


class BackendsConfig(ConfigModel):
    uptime_kuma: UptimeKumaConfig = Field(default_factory=UptimeKumaConfig)
    blackbox_probe: BlackboxProbeConfig = Field(default_factory=BlackboxProbeConfig)


class AppConfig(ConfigModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["console", "json"] = "console"
    health_port: int = Field(default=8080, ge=0, le=65535)   # 0 disables /healthz
    sync_interval_seconds: int = Field(default=60, ge=10)
    # Log what would change without touching any backend
    dry_run: bool = False
    # Identifies this instance's monitors, so several clusters can share one
    # Uptime Kuma without pruning each other's monitors. Don't change it once
    # deployed: monitors under the old id would be orphaned, not adopted.
    owner_id: str = Field(default="default", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=40)
    # Annotations are read as <prefix>/<key>, e.g. auto-monitor.96daysleft.com/enabled
    annotation_prefix: str = "auto-monitor.96daysleft.com"
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    backends: BackendsConfig = Field(default_factory=BackendsConfig)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_backends(self) -> "AppConfig":
        if not (self.backends.uptime_kuma.enabled or self.backends.blackbox_probe.enabled):
            raise ValueError("no backend enabled: enable backends.uptimeKuma and/or backends.blackboxProbe")
        return self


def _expand_env(value: Any) -> Any:
    """Recursively substitute ${VAR} / ${VAR:-default} in string values."""
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(f"environment variable '{name}' is referenced in the config but not set")

    return _ENV_REF.sub(replace, value)


def parse_config(raw: Dict[str, Any], source: str = "config") -> AppConfig:
    raw = _expand_env(raw)

    # LOG_LEVEL env var wins over the file, handy for one-off debugging
    if os.getenv("LOG_LEVEL"):
        raw["logLevel"] = os.environ["LOG_LEVEL"]
        raw.pop("log_level", None)

    try:
        return AppConfig.model_validate(raw)
    except ValueError as e:
        raise ConfigError(f"invalid config in {source}:\n{e}") from e


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load the YAML config from ``path``, $CONFIG_FILE, or ./config.yaml."""
    config_path = Path(path or os.getenv("CONFIG_FILE") or DEFAULT_CONFIG_PATH)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw: Dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"could not parse {config_path}: {e}") from e

    return parse_config(raw, str(config_path))
