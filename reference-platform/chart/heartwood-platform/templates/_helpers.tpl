{{- /*
This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT
*/ -}}

{{- define "heartwood.labels" -}}
app.kubernetes.io/name: heartwood-platform
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{- /* Selector labels of one component. Arguments: context, component. */ -}}
{{- define "heartwood.selector" -}}
app.kubernetes.io/name: heartwood-platform
app.kubernetes.io/instance: {{ .context.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- /*
A generated credential, base64-encoded. An existing Secret keeps its value, so an
upgrade does not rotate it. Arguments: context, name, key.
*/ -}}
{{- define "heartwood.credential" -}}
{{- $existing := lookup "v1" "Secret" .context.Release.Namespace .name -}}
{{- if and $existing (hasKey ($existing.data | default dict) .key) -}}
{{- index $existing.data .key -}}
{{- else -}}
{{- randAlphaNum 40 | b64enc -}}
{{- end -}}
{{- end }}

{{- define "heartwood.keycloakUrl" -}}
{{- .Values.keycloak.url | default (printf "http://keycloak.%s.svc.cluster.local:8080" .Release.Namespace) -}}
{{- end }}

{{- /* Security context of a container under the restricted Pod Security Standard. */ -}}
{{- define "heartwood.containerSecurityContext" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end }}

{{- /* Pod security context for a fixed non-root user. Argument: the user and group ID. */ -}}
{{- define "heartwood.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ . }}
runAsGroup: {{ . }}
fsGroup: {{ . }}
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "heartwood.storageClass" -}}
{{- with .Values.storageClassName }}
storageClassName: {{ . }}
{{- end }}
{{- end }}

{{- /* The network of one host address. Argument: an IPv4 or IPv6 address. */ -}}
{{- define "heartwood.hostCidr" -}}
{{- printf "%s/%d" . (ternary 128 32 (contains ":" .)) -}}
{{- end }}
