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

{{- /* The issuer of the realm's tokens, from which the Hub and the control service derive researcher identifiers. */ -}}
{{- define "heartwood.issuer" -}}
{{- printf "%s/realms/heartwood" (include "heartwood.keycloakUrl" .) -}}
{{- end }}

{{- /* The LiteLLM master key, which LiteLLM requires to start with sk-. */ -}}
{{- define "heartwood.litellmMasterKey" -}}
{{- $existing := lookup "v1" "Secret" .Release.Namespace "litellm" -}}
{{- if and $existing (hasKey ($existing.data | default dict) "master-key") -}}
{{- index $existing.data "master-key" -}}
{{- else -}}
{{- printf "sk-%s" (randAlphaNum 48) | b64enc -}}
{{- end -}}
{{- end }}

{{- define "heartwood.keycloakUrl" -}}
{{- .Values.keycloak.url | default (printf "https://keycloak.%s.svc.cluster.local:8443" .Release.Namespace) -}}
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

{{- /*
The addresses under which pods reach the Kubernetes API server, as a JSON list of
{cidr, port}. Without configured addresses, the Service address and the endpoints of the
API server are read from the cluster.
*/ -}}
{{- define "heartwood.apiServer" -}}
{{- $apiServer := .Values.sessions.apiServer -}}
{{- if not $apiServer -}}
{{- $service := lookup "v1" "Service" "default" "kubernetes" -}}
{{- if $service -}}
{{- $apiServer = append $apiServer (dict "cidr" (include "heartwood.hostCidr" $service.spec.clusterIP) "port" 443) -}}
{{- end -}}
{{- $slice := lookup "discovery.k8s.io/v1" "EndpointSlice" "default" "kubernetes" -}}
{{- if $slice -}}
{{- range $endpoint := $slice.endpoints -}}
{{- range $address := $endpoint.addresses -}}
{{- $apiServer = append $apiServer (dict "cidr" (include "heartwood.hostCidr" $address) "port" (index $slice.ports 0).port) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not $apiServer -}}
{{- fail "sessions.apiServer is empty and the API server addresses cannot be read from the cluster" -}}
{{- end -}}
{{- toJson $apiServer -}}
{{- end }}

{{- /*
Egress of the Funnel server and workers: DNS and the API server.
Arguments: context, apiServer (a list of {cidr, port}).
*/ -}}
{{- define "heartwood.jobsEgress" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- range .apiServer }}
- to:
    - ipBlock:
        cidr: {{ .cidr }}
  ports:
    - protocol: TCP
      port: {{ int .port }}
{{- end }}
{{- end }}

{{- /* The environment of a versitygw admin client with the root account. */ -}}
{{- define "heartwood.versitygwAdmin" -}}
- name: ADMIN_ENDPOINT_URL
  value: https://versitygw:7070
# The admin client verifies the store against the platform CA alone, mounted here.
- name: SSL_CERT_FILE
  value: /etc/platform-ca/ca.crt
- name: ADMIN_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: versitygw-root
      key: access-key
- name: ADMIN_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: versitygw-root
      key: secret-key
{{- end }}
