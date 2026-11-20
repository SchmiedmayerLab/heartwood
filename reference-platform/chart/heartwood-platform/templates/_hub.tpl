{{- /*
This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT
*/ -}}

{{- /*
The volume mounts of a gateway pod's container and init container. The Hub's settings
and the admission policy heartwood-hub-pods both use these lists, so the Hub creates
exactly the mounts that the policy admits.
*/ -}}
{{- define "heartwood.hubMounts" -}}
{{- $main := list (dict "name" "work" "mountPath" "/etc/heartwood/deployment" "subPath" "deployment" "readOnly" true) (dict "name" "work" "mountPath" "/etc/heartwood/native" "subPath" "native" "readOnly" false) (dict "name" "home" "mountPath" "/research" "subPath" "research" "readOnly" false) (dict "name" "home" "mountPath" "/var/lib/heartwood" "subPath" "state" "readOnly" false) (dict "name" "tmp" "mountPath" "/tmp" "subPath" "" "readOnly" false) -}}
{{- $init := list (dict "name" "source" "mountPath" "/etc/heartwood/source" "subPath" "" "readOnly" true) (dict "name" "work" "mountPath" "/etc/heartwood/work" "subPath" "" "readOnly" false) (dict "name" "home" "mountPath" "/var/lib/heartwood-home" "subPath" "" "readOnly" false) -}}
{{- dict "main" $main "init" $init | toJson -}}
{{- end }}

{{- /* A CEL condition that a container's mounts are exactly the given ones. Arguments: container, mounts. */ -}}
{{- define "heartwood.exactMounts" -}}
{{- $own := printf "%s.volumeMounts.filter(m, !m.name.startsWith('kube-api-access-'))" .container -}}
has({{ .container }}.volumeMounts) && size({{ $own }}) == {{ len .mounts }}
        && {{ $own }}.all(m, !has(m.subPathExpr) && (!has(m.mountPropagation) || m.mountPropagation == 'None'))
        && {{ .container }}.volumeMounts.all(m, !m.name.startsWith('kube-api-access-')
        || (m.mountPath == '/var/run/secrets/kubernetes.io/serviceaccount' && has(m.readOnly) && m.readOnly))
{{- range .mounts }}
        && {{ $own }}.exists(m, m.name == {{ .name | toJson }} && m.mountPath == {{ .mountPath | toJson }}
        && (has(m.subPath) ? m.subPath : '') == {{ .subPath | toJson }} && (has(m.readOnly) && m.readOnly) == {{ .readOnly }})
{{- end }}
{{- end }}
