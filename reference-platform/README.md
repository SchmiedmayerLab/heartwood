<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Kubernetes Reference Platform

This directory holds code of the Kubernetes reference platform, a deployment for synthetic and open data.
It is kept apart from the Heartwood application packages under `packages/`.

## Layout

| Path | Content |
|---|---|
| `chart/heartwood-platform/` | Helm chart of the platform; `values-kind.yaml` and `values-gke.yaml` hold the cluster-specific values |
| `cluster/` | kind cluster fixture: configuration, creation and deletion scripts, the network policy controls and the chart installation script |
| `infrastructure/gke/` | OpenTofu configuration of the GKE cluster; see its [README](infrastructure/gke/README.md) |
| `services/` | Python project `heartwood-reference-platform` with its own `pyproject.toml` and `uv.lock`; package `heartwood_platform` in `src/`, service and chart template tests in `tests/`, and the `Dockerfile` of the platform services image |
| `tests/cluster/` | Tests that run against a cluster with the chart installed |
| `tools/` | `check_cloud_boundary.py`, the cloud boundary check, and `render_openshell.py` with its values file |

## Separation Rules

- `services/` is its own uv project outside the application workspace.
  No application package can depend on it, and its dependencies stay out of the application lock.
- The platform services may depend on `heartwood-schemas`, by path, and on no other application package.
- Nothing in `packages/` may import `heartwood_platform`.
- Application packages contain no cloud-specific code.

## Cloud Boundary Check

`tools/check_cloud_boundary.py` enforces the separation rules.
It fails when:

- an application package depends on a cloud infrastructure SDK, such as `google-cloud-*`, `google-auth`, `boto3` or `azure-*`, or on `heartwood-reference-platform`;
- a source file under `packages/*/src` imports such an SDK or `heartwood_platform`, including literal `importlib.import_module` and `__import__` calls;
- a source file under `packages/*/src` names a cloud infrastructure host such as `*.googleapis.com`, `*.amazonaws.com` or `*.azure.com`, a cloud metadata host or address, a GKE annotation or label, or a cloud storage class;
- the platform services depend on or import an application package other than `heartwood-schemas`.

Research platform adapters and model provider names in model routing are outside its scope.
Exceptions are listed in `ALLOWLIST` in the script, each with its reason.
An exception that no longer matches a finding also fails the check.

## Chart

The chart `chart/heartwood-platform` installs the platform into the namespace `heartwood-platform` and creates the namespace `heartwood-sessions`.
The platform is for synthetic and open data only.

| Component | Content |
|---|---|
| PostgreSQL | One StatefulSet with a role and a database for Keycloak |
| Keycloak | One Deployment that imports the realm `heartwood` at its first start |
| Control service | One StatefulSet with the project registry, the launch interface and the provisioning of session namespaces |
| versitygw | S3-compatible object store with one bucket `hw-project-<id>` for each entry in `projects` |
| Platform certificate authority | Secret `platform-ca` with the key, and ConfigMap `platform-ca` with the certificate, which the control service copies into each session namespace |
| Session roles | Six fixed ClusterRoles for session namespaces, and the ServiceAccount and rights of the control service |
| Admission policies | `heartwood-control-scope`, `heartwood-staging-shape` and `heartwood-session-services` |
| OpenShell objects | `files/openshell-session.json`, the objects of one session namespace |

Every image in `values.yaml` is pinned by digest or has no default, and `values.schema.json` rejects an image reference without a digest.
`images.platformServices` has no default and is set at installation to the image built from `services/Dockerfile`.
`images.openshellGateway` has no default either.
`values-kind.yaml` sets it to the published OpenShell 0.1.9 gateway and `sessions.stagingImages` to a small test image.
Both are stand-ins, used when the chart is rendered or installed without the built images.
The [Kubernetes Qualification workflow](../.github/workflows/kubernetes-qualification.yml) installs the chart with the gateway that `images/worker/candidate/gateway.json` builds with its recorded patches and with the worker image and the replacement worker image of the qualification by digest in `sessions.stagingImages`.
`images.openshellRepository` names a repository, such as a mirror, from which session pods pull the OpenShell supervisor and sandbox runtime images under their upstream names and digests; empty pulls them from `ghcr.io/nvidia/openshell`.
Helm generates every credential at the first installation and keeps the existing value on an upgrade.
The chart renders no credential from a fixed value.
PostgreSQL keeps the credentials of the first installation and the Keycloak realm on its volume, so a new installation after `helm uninstall` needs the volume claims of the old one deleted first.

### Keycloak Realm

The realm import creates the client scopes `svc-control`, `svc-data` and `svc-jobs`, each with one audience, and one scope `project-<id>` for each project.
It creates the client `heartwood-control` with the role `manage-clients` and the optional scope `kc-admin-api`, the audience clients `heartwood-data` and `heartwood-jobs`, and the public client `heartwood-cli`.
`files/keycloak-client-scopes.json` holds the built-in client scopes of the pinned Keycloak release, because a realm file that lists client scopes replaces the built-in ones.
The Job `keycloak-bootstrap` disables the temporary administrator that Keycloak creates at its first start.
Keycloak skips the import when the realm exists, so a later change to the realm needs a temporary administrator from `kc.sh bootstrap-admin`.

### Control Service

The control service keeps the project registry in a SQLite database on its volume: projects with their billing binding and policy, their members, and one session for each researcher and project.
The chart value `projects` is the source of projects and members, and the service replaces the registry's projects with it at each start.

| Request | Caller | Result |
|---|---|---|
| `GET /v1/researchers/{subject}/projects` | Platform component | The projects that the researcher is a member of |
| `PUT /v1/researchers/{subject}/projects/{project}/launch` | Platform component | The session with its namespace, the project client and the launch context; the session is created at the first launch |
| `DELETE /v1/researchers/{subject}/projects/{project}/launch` | Platform component | The session, its namespace and its redirect URI are removed |
| `GET /v1/launch-context` | Heartwood gateway | The launch context of the researcher and project that the token names |

A platform component sends a projected service account token with the audience `heartwood-control`.
The service verifies it with a TokenReview and accepts the service accounts in `control.serviceCallers`.
A Heartwood gateway sends a Keycloak access token that the project client obtained by token exchange.
The service verifies its signature, issuer, expiry and type, requires `heartwood-control` as its only audience, and takes the project from the authorized party `hw-proj-<project>` and the researcher from the subject.
Every request is checked against the registry, and the billing binding of a launch context comes from the registry only.

At a launch the service creates the Keycloak client `hw-proj-<project>` unless it exists: a confidential client with the authorization code flow and standard token exchange, without direct access grants and without a service account, and with the optional scopes `svc-control`, `svc-data` and `svc-jobs`.
It adds the redirect URI of the researcher's gateway, from `control.redirectUriTemplate`, and serializes the updates of one client, because Keycloak replaces the whole client on an update.
Its Keycloak role `manage-clients` covers every client of the realm.

### Session Namespaces

At each launch the control service brings the namespace `hw-s-<session>` to the same state, and every step can be repeated:

1. It creates the namespace with the labels `heartwood.platform/session` and `pod-security.kubernetes.io/enforce=restricted`, or reuses a namespace that carries them and `pod-security.kubernetes.io/enforce-version=latest`.
2. It binds `heartwood-session-control` to itself there, and applies the ResourceQuota and LimitRange from `sessions.quota`, `sessions.containerDefaults` and `sessions.containerDefaultRequests`.
   It also applies the ServiceAccount `hw-staging` without a mounted token, for the staging pods of the Heartwood gateway, and the ConfigMap `platform-ca` with the platform CA certificate.
3. It applies the network policies: no ingress and no egress by default; ingress to the OpenShell gateway on port 8080 from the supervisors of its sandboxes and from pods in `heartwood-sessions` that carry the session label; egress of the OpenShell gateway and its certificate Job to DNS and to the Kubernetes API server.
4. It creates the Secret `openshell-kek` with a new 32-byte key, unless the session has one. OpenShell encrypts the provider credentials of the session with it.
5. It binds the other fixed ClusterRoles with RoleBindings and applies the OpenShell objects with server-side apply, with the namespace and `images.openshellGateway` in place of the placeholders, and `images.openshellRepository`, when set, in place of the repository of the supervisor and sandbox runtime images.
6. It sets the subjects of `heartwood-openshell-node-reader` to the OpenShell ServiceAccounts of all sessions in the registry whose removal has not begun.
7. It creates the ServiceAccount `hw-gw-<session>` in `heartwood-sessions`, waits until the OpenShell gateway is ready, and replaces the Secret `hw-gw-<session>`.

The Secret `hw-gw-<session>` holds `launch.json`, the secret of the project client, and the OpenShell client certificate, key and CA certificate.
`launch.json` is the reply to the launch request: the session, the launch context, the namespace with its UID, the UID of `kube-system`, the gateway ServiceAccount and the OpenShell endpoint.
The reply carries no credential.

Deleting the launch removes the session's subject from `heartwood-openshell-node-reader`, the ServiceAccount and Secret `hw-gw-<session>`, and the namespace with everything in it.

The API server addresses of the egress policy come from `sessions.apiServer`.
When that list is empty, Helm reads the address of the Service `kubernetes` and its endpoints from the cluster at installation, and rendering without a cluster fails.
The addresses are written at installation and at each upgrade.
A launch and a deletion of the same researcher and project run one after the other.
The certificates and the key of a session do not rotate while the session exists.
The control service reads the pods of every session namespace.

### Network Policies

Both namespaces deny ingress by default.
PostgreSQL accepts connections from Keycloak.
Keycloak accepts connections from `heartwood-sessions`, from the control service and from its bootstrap Job.
The control service accepts connections from `heartwood-sessions`.
versitygw accepts connections from its bootstrap Job.

### Session Roles and Admission Policies

Session namespaces are named `hw-s-<session>`.
The control service binds the fixed ClusterRoles inside one session namespace with RoleBindings, and no principal creates Roles.
RBAC cannot name namespaces that are created at run time, so ValidatingAdmissionPolicies with failure policy `Fail` confine the principals that act in them.

`heartwood-control-scope` applies to the ServiceAccount `heartwood-control`:

- It writes only in session namespaces, ServiceAccounts and Secrets named `hw-gw-<session>` in `heartwood-sessions`, the subjects of the ClusterRoleBinding `heartwood-openshell-node-reader`, and token reviews.
- It creates and deletes only namespaces named `hw-s-<session>`, and a new one carries the label `heartwood.platform/session` and `pod-security.kubernetes.io/enforce=restricted`, at the latest version of that standard.
- It writes only Secrets of the type `Opaque`.
- A RoleBinding pairs a fixed ClusterRole with its only allowed subject:

  | ClusterRole | Subject |
  |---|---|
  | `heartwood-openshell-sandbox`, `heartwood-openshell-peer` | ServiceAccount `openshell` of the same namespace |
  | `heartwood-openshell-certgen` | ServiceAccount `openshell-certgen` of the same namespace |
  | `heartwood-session-gateway` | ServiceAccount `hw-gw-<session>` in `heartwood-sessions` |
  | `heartwood-session-control` | ServiceAccount `heartwood-control` |

- A change to `heartwood-openshell-node-reader` adds or removes only ServiceAccounts named `openshell` in session namespaces, and changes no label, annotation, owner or finalizer.

`heartwood-staging-shape` applies to every ServiceAccount `hw-gw-*` in `heartwood-sessions`:

- A pod is named `hw-staging-*` and carries only the labels `heartwood.project`, `heartwood.attempt` and `heartwood.role` with the value `staging`, so no NetworkPolicy with allowances selects it.
- A pod uses the ServiceAccount `hw-staging` without a mounted token, runs only an image of `sessions.stagingImages`, runs as user and group `10002` with no other group and the supplemental group policy `Strict`, with the default seccomp profile, a read-only root file system and no capabilities, and mounts only volume claims named `hw-attempt-*`.
- A pod takes no environment value from a Secret, a ConfigMap or a field, and names no node, priority class or runtime class.
- A volume claim is named `hw-attempt-*`, uses the StorageClass `heartwood-attempt` and names no data source, volume or selector.
- An exec session opens only into pods named `hw-staging-*`. Attach and port-forward requests are denied.

`sessions.stagingImages` lists the worker images of the sessions' deployment manifests; during a change of the worker image it lists the current and the next image, and an empty list denies every pod.

`heartwood-session-services` applies to every Service in a namespace that carries the label `heartwood.platform/session`:
the Service has the type `ClusterIP` and no external address.

### OpenShell Objects

`tools/render_openshell.py` renders the OpenShell chart 0.1.9, pinned by the SHA-256 digest of its archive, with the values in `tools/openshell-values.yaml`, and writes `files/openshell-session.json`.
The chart's RBAC creation is switched off, and the tool removes the Role and RoleBinding of the certificate Job.
Every object carries a namespace placeholder, and the containers of the gateway and of the certificate Job carry an image placeholder.
With `supervisor.network.additionalCaConfigMapName: platform-ca`, the gateway mounts the certificate of the session's ConfigMap `platform-ca`, and the Kubernetes driver gives it to the supervisor pod of each sandbox, which trusts it for the HTTPS destinations that the sandbox policy allows.
The driver keeps the certificate in ConfigMaps that it creates, so `heartwood-openshell-sandbox` grants `get` and `create` on ConfigMaps, as the chart's Role does with this value.
Admission accepts a ConfigMap volume of a supervisor pod only under the driver's name `openshell-additional-ca-*`.
The OpenShell gateway, which already controls the supervisors, writes these ConfigMaps.
The tool fails when the chart renders another object kind, another image, or the namespace in a new place.

```bash
cd reference-platform/services
uv run python ../tools/render_openshell.py          # writes the file
uv run python ../tools/render_openshell.py --check  # compares it with a fresh render
```

## Checks

The [Reference Platform workflow](../.github/workflows/reference-platform.yml) runs the cloud boundary check, the offline OpenTofu checks, the platform service checks, the chart template tests and the OpenShell render comparison.
To run the same checks locally, with Helm and OpenSSL installed:

```bash
python3 reference-platform/tools/check_cloud_boundary.py
tofu -chdir=reference-platform/infrastructure/gke fmt -check -recursive
tofu -chdir=reference-platform/infrastructure/gke init -backend=false -lockfile=readonly
tofu -chdir=reference-platform/infrastructure/gke validate
cd reference-platform/services
uv sync --locked
uv run ruff format --check ..
uv run ruff check ..
uv run mypy
uv run pytest
uv run python ../tools/render_openshell.py --check
```

## Local Cluster

The scripts in `cluster/` create the cluster that the [Kubernetes Qualification workflow](../.github/workflows/kubernetes-qualification.yml) uses.
They need Docker, kind, kubectl, Helm, curl, OpenSSL and jq.
kind writes the cluster's credentials to the kubeconfig that `KUBECONFIG` selects, and every `kubectl` call names the cluster's context.

```bash
export KUBECONFIG="${TMPDIR:-/tmp}/heartwood-kubeconfig"
bash reference-platform/cluster/create-cluster.sh heartwood
bash reference-platform/cluster/network-policy-controls.sh heartwood test-results/kubernetes-qualification
docker build --file reference-platform/services/Dockerfile --tag heartwood-platform-services:candidate .
image="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-platform-services:candidate)"
bash reference-platform/cluster/install-chart.sh heartwood --set "images.platformServices=${image}"
(cd reference-platform/services && uv run pytest -c pyproject.toml ../tests/cluster --cluster-context kind-heartwood)
bash reference-platform/cluster/delete-cluster.sh heartwood
```

`create-cluster.sh` installs Calico and the Agent Sandbox controller from pinned manifests whose SHA-256 digests it verifies.
It also starts a registry on the kind network.
`push-image.sh` pushes a local image to that registry and prints the reference by digest under which the cluster pulls it.
Given a repository as a third argument, it copies an image named by digest from a remote registry to that repository in the cluster's registry, with every platform of its index, and prints the reference under the same digest.
`network-policy-controls.sh` runs a reachable-connection control and two blocked-connection controls, one denied by an ingress policy and one by an egress policy.
A blocked control passes only after three consecutive connection timeouts.
The reachable control then runs again, and after the policies are deleted both blocked pods must connect.
The script writes `network-policy-controls.json` and exits with an error unless every control has its expected result.

`install-chart.sh` installs the chart with `values-kind.yaml` and waits until every component is ready.
When the chart is installed with other `sessions.stagingImages`, pass one of them to the cluster tests with `--staging-image`; the default is the value in `values-kind.yaml`.
The pods that the cluster tests run use a pinned agnhost image, independent of `sessions.stagingImages`.
The cluster tests send each admission policy admitted requests and one denied request for each condition, including each ClusterRole with a subject other than its own, as the confined ServiceAccount.
They also check which connections the network policies allow, and call the control service as a stand-in for the spawner.
A launched session is checked for its namespace objects, its RoleBindings, a ready OpenShell gateway, an unchanged state after a second launch, and complete removal.
Requests sent from the control service's pod with its own token show that the admission policy confines it.

### Worker Qualification

`deploy/qualify-kubernetes-worker.py` runs the managed worker on the OpenShell Kubernetes driver in a session that the control service provisions.
It needs the candidate wheels and the gateway build of `deploy/build-worker-candidate.py`, Python 3.12 or later, and the tools above.
The commands below replace the chart installation and the cluster tests of the previous block.

```bash
bash images/worker/build-candidate-images.sh test-results/worker-candidate "$(git rev-parse HEAD)"
python3 deploy/qualify-kubernetes-worker.py images --cluster heartwood \
  --worker-image heartwood-worker:qualification --runtime-image heartwood-runtime:qualification \
  --gateway-artifacts test-results/gateway-candidate --output test-results/kubernetes-qualification/worker
gateway="$(jq --raw-output .image_references.gateway test-results/kubernetes-qualification/worker/images.json)"
worker="$(jq --raw-output .image_references.worker test-results/kubernetes-qualification/worker/images.json)"
openshell="$(jq --raw-output .openshell_repository test-results/kubernetes-qualification/worker/images.json)"
staging="$(jq --compact-output '[.image_references.worker, .image_references.replacement_worker]' \
  test-results/kubernetes-qualification/worker/images.json)"
bash reference-platform/cluster/install-chart.sh heartwood --set "images.platformServices=${image}" \
  --set "images.openshellGateway=${gateway}" --set "images.openshellRepository=${openshell}" \
  --set-json "sessions.stagingImages=${staging}"
(cd reference-platform/services && uv run pytest -c pyproject.toml ../tests/cluster \
  --cluster-context kind-heartwood --staging-image "${worker}")
python3 deploy/qualify-kubernetes-worker.py run --cluster heartwood \
  --output test-results/kubernetes-qualification/worker
```

`images` verifies the gateway build, packages the gateway image, builds the controller image and pushes both with the worker image to the cluster's registry.
It also copies the supervisor and sandbox runtime images of the OpenShell release to the cluster's registry under their digests, so the cluster pulls every image of a worker from there, and builds and pushes the symbolic link workspace and replacement worker images of the Docker qualification.
`run` launches a session as the spawner would, starts the controller pod with that session's gateway ServiceAccount, runs the cases and removes the session.
[Kubernetes Driver Qualification](../documentation/architecture/testing.md#kubernetes-driver-qualification) lists the cases and the evidence.
