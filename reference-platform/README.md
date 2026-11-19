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
| `services/` | Python project `heartwood-reference-platform` with its own `pyproject.toml` and `uv.lock`; package `heartwood_platform` in `src/`, service and chart template tests in `tests/` |
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
| versitygw | S3-compatible object store with one bucket `hw-project-<id>` for each entry in `projects` |
| Platform certificate authority | Secret `platform-ca` with the key, and ConfigMap `platform-ca` with the certificate |
| Session roles | Six fixed ClusterRoles for session namespaces, and the ServiceAccount and rights of the control service |
| Admission policies | `heartwood-control-scope` and `heartwood-staging-shape` |
| OpenShell objects | `files/openshell-session.json`, the objects of one session namespace |

Every image is pinned by digest in `values.yaml`, and `values.schema.json` rejects an image reference without a digest.
Helm generates every credential at the first installation and keeps the existing value on an upgrade.
The chart renders no credential from a fixed value.
PostgreSQL and Keycloak keep the credentials of the first installation on their volumes, so a new installation after `helm uninstall` needs the volume claims of the old one deleted first.

### Keycloak Realm

The realm import creates the client scopes `svc-control`, `svc-data` and `svc-jobs`, each with one audience, and one scope `project-<id>` for each project.
It creates the client `heartwood-control` with the role `manage-clients` and the optional scope `kc-admin-api`, the audience clients `heartwood-data` and `heartwood-jobs`, and the public client `heartwood-cli`.
`files/keycloak-client-scopes.json` holds the built-in client scopes of the pinned Keycloak release, because a realm file that lists client scopes replaces the built-in ones.
The Job `keycloak-bootstrap` disables the temporary administrator that Keycloak creates at its first start.
Keycloak skips the import when the realm exists, so a later change to the realm needs a temporary administrator from `kc.sh bootstrap-admin`.

### Network Policies

Both namespaces deny ingress by default.
PostgreSQL accepts connections from Keycloak.
Keycloak accepts connections from `heartwood-sessions` and from its bootstrap Job.
versitygw accepts connections from its bootstrap Job.

### Session Roles and Admission Policies

Session namespaces are named `hw-s-<session>`.
The control service binds the fixed ClusterRoles inside one session namespace with RoleBindings, and no principal creates Roles.
RBAC cannot name namespaces that are created at run time, so two ValidatingAdmissionPolicies with failure policy `Fail` confine the two principals that act in them.

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

- A change to `heartwood-openshell-node-reader` adds or removes only ServiceAccounts named `openshell` in session namespaces.

`heartwood-staging-shape` applies to every ServiceAccount `hw-gw-*` in `heartwood-sessions`:

- A pod uses the ServiceAccount `hw-staging` without a mounted token, runs only the image `sessions.stagingImage`, runs as a non-root user with the default seccomp profile, a read-only root file system and no capabilities, and mounts only volume claims named `hw-attempt-*` and `emptyDir` volumes.
- A pod takes no environment value from a Secret, a ConfigMap or a field, and names no node, priority class or runtime class.
- A volume claim is named `hw-attempt-*`, uses the StorageClass `heartwood-attempt` and names no data source, volume or selector.
- An exec session opens only into pods named `hw-staging-*`. Attach and port-forward requests are denied.

An empty `sessions.stagingImage` denies every pod.
A defect in `heartwood-control-scope` would let the control service bind the fixed ClusterRoles, which include pod creation, in other namespaces.

### OpenShell Objects

`tools/render_openshell.py` renders the OpenShell chart 0.1.9, pinned by the SHA-256 digest of its archive, with the values in `tools/openshell-values.yaml`, and writes `files/openshell-session.json`.
The chart's RBAC creation is switched off, and the tool removes the Role and RoleBinding of the certificate Job.
Every object carries a namespace placeholder, and both gateway containers carry an image placeholder.
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
bash reference-platform/cluster/install-chart.sh heartwood
(cd reference-platform/services && uv run pytest -c pyproject.toml ../tests/cluster --cluster-context kind-heartwood)
bash reference-platform/cluster/delete-cluster.sh heartwood
```

`create-cluster.sh` installs Calico and the Agent Sandbox controller from pinned manifests whose SHA-256 digests it verifies.
`network-policy-controls.sh` runs a reachable-connection control and two blocked-connection controls, one denied by an ingress policy and one by an egress policy.
A blocked control passes only after three consecutive connection timeouts.
The reachable control then runs again, and after the policies are deleted both blocked pods must connect.
The script writes `network-policy-controls.json` and exits with an error unless every control has its expected result.

`install-chart.sh` installs the chart with `values-kind.yaml` and waits until every component is ready.
The cluster tests send each admission policy one admitted request and one denied request for each condition, including each ClusterRole with a subject other than its own, as the confined ServiceAccount.
They also check which connections the network policies allow.
