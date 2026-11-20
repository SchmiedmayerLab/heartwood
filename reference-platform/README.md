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
| Keycloak | One Deployment that serves HTTPS on port 8443 and imports the realm `heartwood` at its first start |
| Control service | One StatefulSet that serves HTTPS on port 8443, with the project registry, the launch interface and the provisioning of session namespaces |
| versitygw | S3-compatible object store with one bucket `hw-project-<id>` for each entry in `projects` |
| JupyterHub | The Hub, its proxy and the idle culler from the pinned Zero to JupyterHub chart 4.4.5, which start one Heartwood gateway for each researcher and project in `heartwood-sessions` |
| Platform certificate authority | Secret `platform-ca` with the key, ConfigMap `platform-ca` with the certificate, which the control service copies into each session namespace and each gateway Secret, and the Secrets `keycloak-tls` and `heartwood-control-tls` with the serving certificates of Keycloak and the control service |
| Session roles | Six fixed ClusterRoles for session namespaces, and the ServiceAccount and rights of the control service |
| Admission policies | `heartwood-control-scope`, `heartwood-staging-shape` and `heartwood-session-services` |
| OpenShell objects | `files/openshell-session.json`, the objects of one session namespace |

Every image in `values.yaml` is pinned by digest or has no default, and `values.schema.json` rejects an image reference without a digest.
`images.platformServices` has no default and is set at installation to the image built from `services/Dockerfile`.
`images.openshellGateway` and `images.hostedGateway` have no default either.
`images.hostedGateway` is the `hosted-gateway-image` stage of `images/Dockerfile`, and `hub.worker.image` names the worker image by digest.
`values-kind.yaml` sets it to the published OpenShell 0.1.9 gateway and `sessions.stagingImages` to a small test image.
Both are stand-ins, used when the chart is rendered or installed without the built images.
The [Kubernetes Qualification workflow](../.github/workflows/kubernetes-qualification.yml) installs the chart with the gateway that `images/worker/candidate/gateway.json` builds with its recorded patches and with the worker image and the replacement worker image of the qualification by digest in `sessions.stagingImages`.
`images.openshellRepository` names a repository, such as a mirror, from which session pods pull the OpenShell supervisor and sandbox runtime images under their upstream names and digests; empty pulls them from `ghcr.io/nvidia/openshell`.
Helm generates every credential at the first installation and keeps the existing value on an upgrade.
The chart renders no credential from a fixed value.
PostgreSQL keeps the credentials of the first installation and the Keycloak realm on its volume, so a new installation after `helm uninstall` needs the volume claims of the old one deleted first.

### Keycloak

Keycloak serves only HTTPS, with a certificate that the platform CA signs for the names of its Service (`keycloak`, `keycloak.heartwood-platform`, and the `.svc` and `.svc.cluster.local` forms), valid for 825 days.
Helm creates the certificate together with the CA and keeps both on an upgrade.
The issuer of every token is `https://keycloak.heartwood-platform.svc.cluster.local:8443/realms/heartwood`, or the `keycloak.url` value.
The control service and the bootstrap Job verify Keycloak against the platform CA alone.
A gateway does the same with `ca_file` in its `[identity]` table.
The control service writes the CA certificate as `platform-ca.crt` into the gateway's Secret `hw-gw-<session>`, and `ca_file` names a regular copy of it, such as one that an init container makes, because a file in a Secret or ConfigMap volume is a symbolic link that the operator file checks refuse.
The certificates are not renewed automatically.
Deleting the Secret `keycloak-tls` or `heartwood-control-tls` and running `helm upgrade` issues a new serving certificate, and deleting the Secret `platform-ca` replaces the CA and both serving certificates together.
Keycloak reads its certificate files again every minute, so it serves a new certificate within a few minutes, after the kubelet updates the Secret volume.
After a new CA, the control service and running gateways need a restart once Keycloak and the control service serve the new certificates.
The copies of the CA certificate in session namespaces and gateway Secrets are rewritten at the next launch of each session.
A `keycloak.url` with another host name needs a certificate for that name, which the chart does not create.

### Keycloak Realm

The realm import creates the client scopes `svc-control`, `svc-data` and `svc-jobs`, each with one audience, and one scope `project-<id>` for each project.
It creates the client `heartwood-control` with the role `manage-clients` and the optional scope `kc-admin-api`, the audience clients `heartwood-data` and `heartwood-jobs`, and the public client `heartwood-cli`.
It creates one account for each entry in `researchers`, with the entry's `subject` as the identifier of the account and the subject of its tokens.
The accounts are synthetic, and `projects[].members` names them by subject.
Helm generates the password of each account at the first installation and stores it in the Secret `keycloak-researchers` under the user name.
`files/keycloak-client-scopes.json` holds the built-in client scopes of the pinned Keycloak release, because a realm file that lists client scopes replaces the built-in ones.
The Job `keycloak-bootstrap` disables the temporary administrator that Keycloak creates at its first start.
Keycloak skips the import when the realm exists, so a later change to the realm needs a temporary administrator from `kc.sh bootstrap-admin`.
An account added to `researchers` after the first installation gets a password in the Secret and no account in Keycloak until it is created that way.

### Control Service

The control service keeps the project registry in a SQLite database on its volume: projects with their billing binding and policy, their members, and one session for each researcher and project.
The chart value `projects` is the source of projects and members, and the service replaces the registry's projects with it at each start.

The service serves only HTTPS on port 8443, with a certificate that the platform CA signs for the names of its Service (`heartwood-control`, `heartwood-control.heartwood-platform`, and the `.svc` and `.svc.cluster.local` forms), valid for 825 days and kept on an upgrade like the certificate of Keycloak.
It checks its certificate files every minute and serves a changed certificate to new connections without a restart; a certificate that does not load with its key leaves the previous one in use.
Its callers verify it against the platform CA alone.
A Heartwood gateway reaches its MCP endpoint as the planning service of its `[planning]` table, with `ca_file` naming a copy of `platform-ca.crt`, `private_address`, because the Service has a cluster address, and a `launch_context_file` holding the `launch_context` member of `launch.json`.

| Request | Caller | Result |
|---|---|---|
| `GET /v1/researchers/{subject}/projects` | Platform component | The projects that the researcher is a member of |
| `PUT /v1/researchers/{subject}/projects/{project}/launch` | Platform component | The session with its namespace, the project client and the launch context; the session is created at the first launch |
| `DELETE /v1/researchers/{subject}/projects/{project}/launch` | Platform component | The session, its namespace and its redirect URI are removed |
| `GET /v1/launch-context` | Heartwood gateway | The launch context of the researcher and project that the token names |
| `/mcp`, MCP over streamable HTTP | Heartwood gateway | The read-only tool `estimate_workload` |

A platform component sends a projected service account token with the audience `heartwood-control`.
The service verifies it with a TokenReview and accepts the service accounts in `control.serviceCallers`.
A Heartwood gateway sends a Keycloak access token that the project client obtained by token exchange.
The service verifies its signature, issuer, expiry and type, requires `heartwood-control` as its only audience, and takes the project from the authorized party `hw-proj-<project>` and the researcher from the subject.
Every request is checked against the registry, and the billing binding of a launch context comes from the registry only.

The control service prices synthetic work from the price sheet in the chart value `syntheticPrices`.
Each entry of `skuPrices` is a row of the FOCUS SKU Price dataset, with the column names of the draft revision that the [platform contract](../documentation/operate/platform-contract.md#launch-context-budgets-and-estimates) pins, such as `SkuPriceId`, `UnitPrice`, `UnitPriceType`, `PricingUnit`, `PricingCurrency` and the effective period.
The prices in `values.yaml` are synthetic examples, and the sheet claims no FOCUS conformance.
Each entry of `runtimeProfiles` names a runtime profile of workload predictions, the kind of work it runs and the price of each measure: the seconds of exploration, the task seconds of batch work including the retry allowance, and the input and output tokens of inference.
A GPU job is batch work on a GPU runtime profile, and the runtime profile of inference is the model name.
`quantityPerPricingUnit` converts a measure to the pricing unit, such as 1000000 tokens for a price per million tokens.
Prices and quantities are decimal strings with at most 18 integer and 12 fractional digits.
The service does not start with a sheet that has unknown fields, values of another JSON type, prices that are negative or not such decimal strings, duplicate identifiers, rates of unknown prices, a missing measure or more than one currency.

The MCP tool `estimate_workload` takes `project_id`, `scope`, `prediction` and `element_ids` and returns a `heartwood.platform-estimate.v1` document for the whole plan or for the named elements.
The tool is listed as read-only, and an estimate reserves nothing and authorizes nothing.
Each element has a cost range with its coverage.
Work without a price, with a quantity in another unit, without an expected quantity or with a quantity of more digits has an unknown cost with the reason, and the total then covers the priced elements with partial coverage.
Each estimate has a new identity, its creation time, a validity of `estimateValiditySeconds` that ends no later than the prices it used, and a FOCUS evidence reference with a digest for each price it used.
Expected costs and upper bounds are rounded up and lower bounds down to six decimal places, so an expected cost or upper bound above zero is never reported as `"0"`.
A prediction has at most 500 elements, and estimates are not stored.

Every MCP request carries an exchanged token, verified like the token of `GET /v1/launch-context`, and the researcher must be a member of the token's project.
A request without such a token receives HTTP 401, and a request during which the keys of Keycloak cannot be fetched receives HTTP 502.
The tool refuses a `project_id` other than the token's project, a researcher without a session in the project, and a prediction whose `project_context_id` is not the launch context of that session.
Every price applies to every project, because `SkuPriceEligibility` must be `{"IsGlobalScope": true}`.

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

The Secret `hw-gw-<session>` holds `launch.json`, the secret of the project client, the OpenShell client certificate, key and CA certificate, and the platform CA certificate `platform-ca.crt`.
`launch.json` is the reply to the launch request: the session, the launch context, the namespace with its UID, the UID of `kube-system`, the gateway ServiceAccount and the OpenShell endpoint.
The reply carries no credential.

Deleting the launch removes the session's subject from `heartwood-openshell-node-reader`, the ServiceAccount and Secret `hw-gw-<session>`, and the namespace with everything in it.

The API server addresses of the egress policy come from `sessions.apiServer`.
When that list is empty, Helm reads the address of the Service `kubernetes` and its endpoints from the cluster at installation, and rendering without a cluster fails.
The addresses are written at installation and at each upgrade.
A launch and a deletion of the same researcher and project run one after the other.
The certificates and the key of a session do not rotate while the session exists.
The control service reads the pods of every session namespace.

### JupyterHub

The chart depends on the Zero to JupyterHub chart 4.4.5, and the chart tests compare the SHA-256 digest of its archive `charts/jupyterhub-4.4.5.tgz` with the digest that the chart repository index lists for it.
The Hub, the proxy `proxy-public` and the idle culler run in `heartwood-platform`; the chart's own RBAC, user scheduler, image pullers, placeholders and network policies are off.
`files/hub/heartwood_hub.py` configures the Hub, which loads it from its `jupyterhub_config.d` directory and reads its settings from the ConfigMap `heartwood-hub` on each spawn.
A change of that file takes effect after the Hub restarts.

Researchers sign in to the Hub through Keycloak with the client `heartwood-hub`, whose secret Helm generates into the Secret `heartwood-hub-keycloak`.
The Hub trusts the platform CA alone for Keycloak and the control service, without the certificate directory that libcurl reads by default, and its user name is the subject of the researcher's token.
Every account of the realm may sign in; the control service decides which projects a researcher may start.

Each project of a researcher is one named server whose name is the project.
The spawn form offers a server only the project of its name, after the Hub asks the control service for the researcher's projects with a projected token of the audience `heartwood-control`.
Before each start the Hub asks the control service to prepare the session, and it refuses the start when the researcher is not a member of the project.
It then starts the gateway pod in `heartwood-sessions` with the ServiceAccount `hw-gw-<session>`, the session label, the volume claim `hw-home-<session>` and the hosted gateway image.
The init container runs `heartwood-hosted-configure`, which copies the Secret `hw-gw-<session>` into a memory volume with owner-only files, registers the session's OpenShell gateway with its client certificate, and creates the research and state directories on the claim.
The gateway mounts the copies read-only at `/etc/heartwood/deployment` and the registration at `/etc/heartwood/native` from the memory volume, and the research and state directories at `/research` and `/var/lib/heartwood` from the claim; `templates/_hub.tpl` lists these mounts.
The gateway serves `heartwood gateway serve` with `identity-proxy` ingress under `/user/<subject>/<project>`, and accepts proxied requests only from `hub.trustedProxySources`.

The control service writes the gateway's deployment manifest into the Secret: the identity of the researcher and the project client, the worker on the Kubernetes driver in the session namespace, and the control service as the `[planning]` service.
`hub.worker` sets the worker image, its sandbox policy and the policy's effective digest from qualification, and `hub.modelRoute` the model route; the default route names an address that does not resolve.
The default policy digest matches no policy, so no worker is admitted until qualified values are set.

The idle culler stops a server after `jupyterhub.cull.timeout` seconds without traffic through the proxy.
The server's volume claim, the session namespace and the OpenShell gateway stay, and the next start runs the same session; the researcher signs in again, because tokens live in the gateway's memory only.
Deleting a named server deletes the session at the control service and the server's volume claim.

The Role `heartwood-hub-spawner` in `heartwood-sessions` grants the Hub what KubeSpawner needs: pods `create`, `delete`, `list` and `watch`, volume claims `create`, `delete` and `get`, and events `list` and `watch`.
It grants no access to Secrets or Services; KubeSpawner touches Secrets and Services only with internal TLS or per-server Services, which the chart leaves off.
The Hub remains a trusted platform component: it can start the gateway of any session with that session's ServiceAccount and Secret, and delete pods and volume claims in `heartwood-sessions`.

`heartwood-hub-pods` applies to the Hub's ServiceAccount in `heartwood-sessions`:

- A pod carries its session label and the role label `hosted-gateway`, and uses the ServiceAccount `hw-gw-<session>` of that session.
- A pod runs one init container and one container of `images.hostedGateway`; the init container runs only `heartwood-hosted-configure`, and the container only `heartwood gateway serve` with the fixed arguments above.
- A pod carries only the labels of a JupyterHub server and its session labels, and only annotations under `hub.jupyter.org/`.
- A pod mounts only its session's Secret, a memory volume, a temporary volume, the volume claim `hw-home-<session>` and the standard ServiceAccount token volume, and each container mounts exactly the volumes, paths and subdirectories of the layout above, so only the init container mounts the Secret and the copies stay in the memory volume.
- A pod sets only environment variables of the names that JupyterHub and KubeSpawner use and `HOME`, with literal values, has no lifecycle hook, no exec probe, no probe of another host and the default termination message, and resolves names through the cluster's DNS only.
- A pod runs as user `10001` without host access, with the default seccomp profile, a read-only root file system and no capabilities.
- A volume claim is named `hw-home-<session>` and names no data source, volume or selector.

`heartwood-sessions` enforces the restricted Pod Security Standard.

### Network Policies

Both namespaces deny ingress by default.
PostgreSQL accepts connections from Keycloak.
Keycloak accepts connections from `heartwood-sessions`, from the Hub, from the control service and from its bootstrap Job.
The control service accepts connections from `heartwood-sessions` and from the Hub.
versitygw accepts connections from its bootstrap Job.
The proxy accepts researchers' connections on port 8000 from anywhere and configuration from the Hub on port 8001, and the Hub accepts connections from the proxy.
A gateway pod accepts connections on port 8888 from the proxy and the Hub, so gateways reach each other only through the proxy, where each gateway verifies its researcher.

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
docker build --file images/Dockerfile --target hosted-gateway-image --tag heartwood-hosted-gateway:candidate .
hosted="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-hosted-gateway:candidate)"
bash reference-platform/cluster/install-chart.sh heartwood --set "images.platformServices=${image}" \
  --set "images.hostedGateway=${hosted}"
(cd reference-platform/services && uv run pytest -c pyproject.toml ../tests/cluster --cluster-context kind-heartwood \
  --ignore ../tests/cluster/test_hub_spawning.py)
bash reference-platform/cluster/delete-cluster.sh heartwood
```

The Hub's tests in `test_hub_spawning.py` start Heartwood gateways, which require an OpenShell gateway with the recorded patches, so they run with the images of [Worker Qualification](#worker-qualification).

`create-cluster.sh` installs Calico and the Agent Sandbox controller from pinned manifests whose SHA-256 digests it verifies.
It also starts a registry on the kind network.
The nodes reach it as `<name>-registry.internal`, a host name with a dot, which the worker deployment requires of an image repository.
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
A synthetic researcher signs in to a project client with the authorization code flow, and the client exchanges the researcher's token for a token of one service.
The tests check the subject, the authorized party and the single audience of the exchanged token, and that Keycloak refuses an audience without its scope and the client of another project.
The command line client's device sign-in with the scope `project-demo` gives a token that the client of project demo exchanges with HTTP Basic client authentication, as the gateway does, and that the client of project other cannot exchange.
The tests check that the control service refuses the researcher's own token, a token for another service, and a researcher who is not a member of the client's project.
With an exchanged token the estimate tool prices a synthetic plan from the chart's price sheet.
The tests check that it refuses a request without a token for the control service, another project, and the launch context of another project.

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
hosted="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-hosted-gateway:qualification)"
bash reference-platform/cluster/install-chart.sh heartwood --set "images.platformServices=${image}" \
  --set "images.openshellGateway=${gateway}" --set "images.openshellRepository=${openshell}" \
  --set "images.hostedGateway=${hosted}" --set "hub.worker.image=${worker}" \
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
