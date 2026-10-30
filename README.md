# auto-monitor-k8s

Watches the cluster's routing resources and keeps monitoring tools in sync
with the URLs they expose. Create an Ingress and a monitor shows up in
Uptime Kuma. Delete the Ingress and the monitor goes away.

- **Sources:** `Ingress` (default), Gateway API `HTTPRoute`, Traefik `IngressRoute`
- **Backends:**
  - **Uptime Kuma 2.x**, over its socket.io API (the one the web UI uses).
  - **Prometheus Operator `Probe`** objects, scraped through a blackbox exporter.

It is a level-triggered loop. Every `syncIntervalSeconds` it lists the
resources, works out the desired set of URLs, and creates, updates or
removes monitors to match. There is no state besides what is in the cluster
and the backends.

## How monitors are owned

- **Uptime Kuma:** each managed monitor's description ends with
  `[auto-monitor-k8s owner=<ownerId> key=<Kind>/<ns>/<name>/<host>]`.
  Monitors without that marker, such as ones you made by hand, are never
  touched. Monitors are tagged `k8s: <namespace>`.
- **Probes:** owned through the labels
  `app.kubernetes.io/managed-by=auto-monitor-k8s` and
  `auto-monitor.96daysleft.com/owner=<ownerId>`.

Give each cluster its own `ownerId` if several share one Uptime Kuma.

Only these fields are enforced on a managed Uptime Kuma monitor: name, URL,
interval, retries, timeout, accepted codes, TLS and the notification list.
Anything else you change in the UI, such as keyword checks or a group, is
kept.

## Which URL gets monitored

| Host                                                                 | URL                           |
| -------------------------------------------------------------------- | ----------------------------- |
| In the Ingress's `spec.tls`, or Traefik `router.tls: "true"`          | `https://<host>/`             |
| Neither                                                              | `<defaultScheme>://<host>/`   |
| Only sub-paths routed                                                | the first routed path         |
| Wildcard (`*.example.com`) or no host                                | skipped                       |
| Same URL from several resources                                      | monitored once                |

Narrow it down with `discovery.includeHosts` / `excludeHosts` (regexes),
`namespaces`, `excludeNamespaces` and `ingressClasses`. You can also switch
`discovery.mode` to `optIn`.

## Annotations

Per resource, with prefix `auto-monitor.96daysleft.com/`:

| Annotation              | Example                            | Effect                                         |
| ----------------------- | ---------------------------------- | ---------------------------------------------- |
| `enabled`               | `"false"`                          | opt out (or opt in, in `optIn` mode)           |
| `name`                  | `Jellyfin`                         | monitor name (default: the host)               |
| `path`                  | `/health`                          | path to check                                  |
| `url`                   | `https://jf.example.com/health`    | check this URL instead                         |
| `hosts`                 | `a.example.com,b.example.com`      | only these hosts of the resource               |
| `interval`              | `"300"`                            | check interval in seconds (min 20)             |
| `accepted-status-codes` | `"200-299,401"`                    | e.g. for apps behind auth                      |
| `ignore-tls`            | `"true"`                           | skip certificate validation                    |
| `backends`              | `uptime-kuma`                      | limit to some backends                         |

```yaml
metadata:
  annotations:
    auto-monitor.96daysleft.com/name: Jellyfin
    auto-monitor.96daysleft.com/path: /health
```

## Install

The chart is published to `oci://ghcr.io/96daysleft/charts/auto-monitor-k8s`.
The files in [examples/](examples/) are ready to drop into `k8s-server`:

- [values-talos.yaml](examples/values-talos.yaml): values for the home lab
  cluster.
- [external-secret.yaml](examples/external-secret.yaml): the Uptime Kuma
  login from OpenBao (kv `auto-monitor-k8s`, keys `UPTIME_KUMA_USERNAME` and
  `UPTIME_KUMA_PASSWORD`).
- [argocd-application.yaml](examples/argocd-application.yaml): the Argo CD
  app.

First install: set `dryRun: true`, read the logs (`Would create monitor ...`),
then turn it off.

Uptime Kuma is single-user, so this logs in with the admin account. That
account must not have 2FA enabled.

## Development

```bash
python -m venv venv && ./venv/bin/pip install -e '.[dev]'
./venv/bin/pytest                      # includes Helm render tests if helm is installed
CONFIG_FILE=config.yaml ./venv/bin/auto-monitor-k8s   # uses your current kubectl context
```

Releases follow the same pattern as the other repos. Push a `vX.Y.Z` tag to
build the image, the Python package and the Helm chart.

## Adding another backend

Subclass `Backend` in [auto_monitor/backends/](auto_monitor/backends/) and
implement `sync(targets)`: make the tool match the target list, touching
only what this `ownerId` owns. Then register it in `build_backends`, give it
a config block in [config.py](auto_monitor/config.py), and add its name to
`BACKEND_NAMES` in [discovery.py](auto_monitor/discovery.py).
