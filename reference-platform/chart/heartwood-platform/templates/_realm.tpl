{{- /*
This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT
*/ -}}

{{- /* A client scope that adds one client to the audience of a token. Arguments: name, audience. */ -}}
{{- define "heartwood.audienceScope" -}}
{{- $mapper := dict "name" "audience" "protocol" "openid-connect" "protocolMapper" "oidc-audience-mapper" "consentRequired" false "config" (dict "included.client.audience" .audience "access.token.claim" "true" "introspection.token.claim" "true") -}}
{{- dict "name" .name "protocol" "openid-connect" "attributes" (dict "include.in.token.scope" "true" "display.on.consent.screen" "false") "protocolMappers" (list $mapper) | toJson -}}
{{- end }}

{{- /*
The realm that Keycloak imports at its first start.
A realm file that lists client scopes replaces the built-in ones, so
files/keycloak-client-scopes.json holds the built-in scopes of the pinned Keycloak
release, and the default scope lists below repeat that release's defaults.
*/ -}}
{{- define "heartwood.realm" -}}
{{- $scopes := .Files.Get "files/keycloak-client-scopes.json" | fromJsonArray -}}
{{- $services := list "control" "data" "jobs" -}}
{{- range $service := $services -}}
{{- $scopes = append $scopes (include "heartwood.audienceScope" (dict "name" (printf "svc-%s" $service) "audience" (printf "heartwood-%s" $service)) | fromJson) -}}
{{- end -}}
{{- $projectScopes := list -}}
{{- range $project := .Values.projects -}}
{{- $projectScopes = append $projectScopes (printf "project-%s" $project.id) -}}
{{- $scopes = append $scopes (include "heartwood.audienceScope" (dict "name" (printf "project-%s" $project.id) "audience" (printf "hw-proj-%s" $project.id)) | fromJson) -}}
{{- end -}}
{{- $defaults := list "basic" "roles" "web-origins" "acr" "profile" "email" -}}
{{- $clients := list -}}
{{- /* The control service: audience of exchanged tokens, and a service account that manages clients. */ -}}
{{- $clients = append $clients (dict "clientId" "heartwood-control" "publicClient" false "secret" "${HEARTWOOD_CONTROL_CLIENT_SECRET}" "standardFlowEnabled" false "directAccessGrantsEnabled" false "serviceAccountsEnabled" true "defaultClientScopes" (append $defaults "service_account") "optionalClientScopes" (list "kc-admin-api")) -}}
{{- range $service := list "data" "jobs" -}}
{{- $clients = append $clients (dict "clientId" (printf "heartwood-%s" $service) "bearerOnly" true) -}}
{{- end -}}
{{- /* The Hub signs researchers in with the authorization code flow. */ -}}
{{- $clients = append $clients (dict "clientId" "heartwood-hub" "publicClient" false "secret" "${HEARTWOOD_HUB_CLIENT_SECRET}" "standardFlowEnabled" true "directAccessGrantsEnabled" false "serviceAccountsEnabled" false "redirectUris" (list (printf "%s/hub/oauth_callback" .Values.hub.publicOrigin)) "defaultClientScopes" $defaults) -}}
{{- /* The command line client signs in with the device authorization grant. */ -}}
{{- $clients = append $clients (dict "clientId" "heartwood-cli" "publicClient" true "standardFlowEnabled" false "directAccessGrantsEnabled" false "attributes" (dict "oauth2.device.authorization.grant.enabled" "true") "defaultClientScopes" $defaults "optionalClientScopes" $projectScopes) -}}
{{- $serviceAccount := dict "username" "service-account-heartwood-control" "enabled" true "serviceAccountClientId" "heartwood-control" "clientRoles" (dict "realm-management" (list "manage-clients")) -}}
{{- $users := list $serviceAccount -}}
{{- /*
Synthetic researchers. The identifier of a user is the subject of its tokens, and the
registry lists members by subject. Keycloak reads each password from its environment.
*/ -}}
{{- range $index, $researcher := .Values.researchers -}}
{{- $password := dict "type" "password" "temporary" false "value" (printf "${HEARTWOOD_RESEARCHER_PASSWORD_%d}" $index) -}}
{{- $users = append $users (dict "id" $researcher.subject "username" $researcher.username "enabled" true "emailVerified" true "email" (printf "%s@example.invalid" $researcher.username) "firstName" "Synthetic" "lastName" "Researcher" "credentials" (list $password)) -}}
{{- end -}}
{{- dict "realm" "heartwood" "enabled" true "clientScopes" $scopes "defaultDefaultClientScopes" (list "role_list" "saml_organization" "AuthnContextClassRef" "profile" "email" "roles" "web-origins" "acr" "basic") "defaultOptionalClientScopes" (list "offline_access" "address" "phone" "microprofile-jwt" "organization") "clients" $clients "users" $users | toPrettyJson -}}
{{- end }}
