{{/*
Expand the name of the chart.
*/}}
{{- define "auto-monitor-k8s.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "auto-monitor-k8s.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "auto-monitor-k8s.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{ include "auto-monitor-k8s.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "auto-monitor-k8s.selectorLabels" -}}
app.kubernetes.io/name: {{ include "auto-monitor-k8s.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}


{{/*
ServiceAccount name
*/}}
{{- define "auto-monitor-k8s.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "auto-monitor-k8s.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Secret holding config.yaml — existingSecret if provided, otherwise the chart-managed one.
*/}}
{{- define "auto-monitor-k8s.secretName" -}}
{{- default (include "auto-monitor-k8s.fullname" .) .Values.existingSecret }}
{{- end }}

{{/*
Namespace the blackbox Probe objects go to (defaults to the release namespace).
*/}}
{{- define "auto-monitor-k8s.probeNamespace" -}}
{{- default .Release.Namespace .Values.backends.blackboxProbe.namespace }}
{{- end }}

{{/*
The application config.yaml, rendered from values.
*/}}
{{- define "auto-monitor-k8s.config" -}}
{{- $kuma := .Values.backends.uptimeKuma }}
{{- $probe := .Values.backends.blackboxProbe }}
{{- if not (or $kuma.enabled $probe.enabled) }}
{{- fail "enable backends.uptimeKuma and/or backends.blackboxProbe (or set existingSecret)" }}
{{- end }}
{{- if and $kuma.enabled (not $kuma.url) }}
{{- fail "backends.uptimeKuma.url is required" }}
{{- end }}
{{- if and $probe.enabled (not $probe.proberUrl) }}
{{- fail "backends.blackboxProbe.proberUrl is required" }}
{{- end }}
{{- $backends := dict
  "uptimeKuma" $kuma
  "blackboxProbe" (merge (dict "namespace" (include "auto-monitor-k8s.probeNamespace" .)) (omit $probe "namespace"))
}}
{{- $config := dict
  "logLevel" .Values.logLevel
  "logFormat" .Values.logFormat
  "healthPort" .Values.healthPort
  "syncIntervalSeconds" .Values.syncIntervalSeconds
  "dryRun" .Values.dryRun
  "ownerId" .Values.ownerId
  "annotationPrefix" .Values.annotationPrefix
  "discovery" .Values.discovery
  "backends" $backends
}}
{{- toYaml $config }}
{{- end }}
