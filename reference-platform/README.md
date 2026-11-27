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
| `images/funnel/` | Build of the Funnel image: `funnel.json` with the pinned source and its recorded patches, the patches and the `Dockerfile` |
| `infrastructure/gke/` | OpenTofu configuration of the GKE cluster; see its [README](infrastructure/gke/README.md) |
| `services/` | Python project `heartwood-reference-platform` with its own `pyproject.toml` and `uv.lock`; package `heartwood_platform` in `src/`, service and chart template tests in `tests/`, and the `Dockerfile` of the platform services image |
| `tests/cluster/` | Tests that run against a cluster with the chart installed |
| `tools/` | `check_cloud_boundary.py`, the cloud boundary check, `render_openshell.py` with its values file, `prepare_funnel.py`, which prepares the Funnel build, and `publish_images.py`, which publishes the platform images and pins them in chart values |

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

The chart `chart/heartwood-platform` installs the platform into the namespace `heartwood-platform` and creates the namespace `heartwood-sessions`, and with the Funnel executor one namespace `hw-jobs-<project>` for each project.
The platform is for synthetic and open data only, as [Permitted Data](#permitted-data) states.

| Component | Content |
|---|---|
| PostgreSQL | One StatefulSet with a role and a database for Keycloak and for LiteLLM |
| Keycloak | One Deployment that serves HTTPS on port 8443 and imports the realm `heartwood` at its first start |
| Control service | One StatefulSet that serves HTTPS on port 8443, with the project registry, the launch interface and the provisioning of session namespaces |
| LiteLLM | The proxy of the model route, one Deployment that serves HTTPS on port 4000, with one virtual key for each session's workers |
| Synthetic model | With `inference.syntheticModel.enabled`, a deterministic model behind LiteLLM for tests |
| Local model | With `inference.localModel.enabled`, `llama-server` from `images.modelRuntime` behind LiteLLM |
| Data service | One StatefulSet that serves HTTPS on port 8443, with the project data catalog, the DRS read endpoints and the MCP catalog tools |
| Job service | With `projects`, one StatefulSet that serves HTTPS on port 8443, with the MCP tools of batch tasks, see [Job Service](#job-service) |
| Job executors | With `jobs.executor` set to `funnel`, one Funnel server from `images.funnel` for each project in `hw-jobs-<project>`, see [Job Executors](#job-executors) |
| versitygw | S3-compatible object store that serves HTTPS on port 7070, with one bucket `hw-project-<id>` for each entry in `projects` |
| JupyterHub | The Hub, its HTTPS proxy and the idle culler from the pinned Zero to JupyterHub chart 4.4.5, which start one Heartwood gateway for each researcher and project in `heartwood-sessions` |
| Platform certificate authority | Secret `platform-ca` with the key, ConfigMap `platform-ca` with the certificate, which the control service copies into each session namespace and each gateway Secret, and the Secrets `keycloak-tls`, `heartwood-control-tls`, `heartwood-data-tls`, `versitygw-tls`, `litellm-tls`, `heartwood-jobs-tls`, `synthetic-model-tls`, `llama-server-tls` and `proxy-tls` with the serving certificates of those services, and the Secret `funnel-server-tls` of each project's Funnel server |
| Session roles | Six fixed ClusterRoles for session namespaces, the ServiceAccount and rights of the control service, the ServiceAccount `heartwood-operator`, which identifies platform operators at the control service and has no Kubernetes rights, and the Role `heartwood-hub-spawner` of the Hub in `heartwood-sessions` |
| Admission policies | `heartwood-control-scope`, `heartwood-staging-shape`, `heartwood-staging-names`, `heartwood-session-services`, `heartwood-hub-pods`, `heartwood-project-data-copy` and `heartwood-funnel-worker-jobs` |
| OpenShell objects | `files/openshell-session.json`, the objects of one session namespace |

Every image in `values.yaml` is pinned by digest or has no default, and `values.schema.json` rejects an image reference without a digest.
`images.platformServices` has no default and is set at installation to the image built from `services/Dockerfile`.
`images.openshellGateway` and `images.hostedGateway` have no default either.
`images.hostedGateway` is the `hosted-gateway-image` stage of `images/Dockerfile`, and `hub.worker.image` names the worker image by digest.
`values-kind.yaml` sets `images.openshellGateway` to the published OpenShell 0.1.9 gateway, `sessions.stagingImages` and `hub.worker.image` to a pinned agnhost test image, and `hub.modelRoute.binaries` to that image's program `/agnhost`.
These four values are stand-ins, used when the chart is rendered or installed without the images that the qualification builds; `images.platformServices` and `images.hostedGateway` are still set at installation.
The [Kubernetes Qualification workflow](../.github/workflows/kubernetes-qualification.yml) installs the chart with the gateway that `images/worker/candidate/gateway.json` builds with its recorded patches, the built hosted gateway image, the worker image of the qualification in `hub.worker.image`, and that worker image and the replacement worker image by digest in `sessions.stagingImages`.
`images.openshellRepository` names a repository, such as a mirror, from which session pods pull the OpenShell supervisor and sandbox runtime images under their upstream names and digests; empty pulls them from `ghcr.io/nvidia/openshell`.
Helm generates every credential at the first installation and keeps the existing value on an upgrade.
The chart renders no credential from a fixed value.
PostgreSQL keeps the credentials of the first installation and the Keycloak realm on its volume, so a new installation after `helm uninstall` needs the volume claims of the old one deleted first.
It creates the roles and databases of Keycloak and LiteLLM only when its volume is new.

### Keycloak

Keycloak serves only HTTPS, with a certificate that the platform CA signs for the names of its Service (`keycloak`, `keycloak.heartwood-platform`, and the `.svc` and `.svc.cluster.local` forms), valid for 825 days.
Helm creates the certificate together with the CA and keeps both on an upgrade.
The issuer of every token is `https://keycloak.heartwood-platform.svc.cluster.local:8443/realms/heartwood`, or the `keycloak.url` value.
The control service and the bootstrap Job verify Keycloak against the platform CA alone.
A gateway does the same with `ca_file` in its `[identity]` table.
The control service writes the CA certificate as `platform-ca.crt` into the gateway's Secret `hw-gw-<session>`, and `ca_file` names a regular copy of it, such as one that an init container makes, because a file in a Secret or ConfigMap volume is a symbolic link that the operator file checks refuse.
The certificates are not renewed automatically.
Deleting the Secret `keycloak-tls`, `heartwood-control-tls`, `heartwood-data-tls` or `versitygw-tls` and running `helm upgrade` issues a new serving certificate, and deleting the Secret `platform-ca` replaces the CA and every serving certificate together.
Keycloak reads its certificate files again every minute, so it serves a new certificate within a few minutes, after the kubelet updates the Secret volume.
After a new CA, the control and data services and running gateways need a restart once Keycloak and the control service serve the new certificates.
versitygw reads its certificate at start, so it needs a restart after its certificate is replaced.
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
A member is an OpenID Connect subject in the form the provider issues it: 1 to 255 characters without control or format characters and without leading or trailing whitespace.

The service's paths, the Hub's user names, the gateways' base paths and the redirect URIs name a researcher by a researcher identifier: `r` followed by 32 hexadecimal digits of the HMAC-SHA-256 of the issuer, a zero byte and the subject, under the key in the Secret `heartwood-researcher-ids`.
So the subject appears neither in these URLs nor in access logs that record them.
The service finds the subject of an identifier among the members and the holders of sessions.
For an identifier that names nobody, it lists no projects, refuses a launch and answers a deletion as done.
Helm generates the key at the first installation, keeps it on upgrades and leaves it in place on `helm uninstall`; a render without cluster access, such as `helm template`, generates a new value.
Only the control service and the Hub mount it.
Replacing the key renames every researcher: researchers sign in to the Hub as new users, the earlier users' servers no longer reach their sessions, and the redirect URIs under the earlier identifiers stay registered with the project clients until an operator removes them.

The service serves only HTTPS on port 8443, with a certificate that the platform CA signs for the names of its Service (`heartwood-control`, `heartwood-control.heartwood-platform`, and the `.svc` and `.svc.cluster.local` forms), valid for 825 days and kept on an upgrade like the certificate of Keycloak.
It checks its certificate files every minute and serves a changed certificate to new connections without a restart; a certificate that does not load with its key leaves the previous one in use.
Its callers verify it against the platform CA alone.
A Heartwood gateway reaches its MCP endpoint as the planning service of its `[planning]` table, with `ca_file` naming a copy of `platform-ca.crt`, `private_address`, because the Service has a cluster address, and a `launch_context_file` holding the `launch_context` member of `launch.json`.

| Request | Caller | Result |
|---|---|---|
| `GET /v1/researchers/{researcher}/projects` | Platform component | The projects that the researcher is a member of |
| `PUT /v1/researchers/{researcher}/projects/{project}/launch` | Platform component | The session with its namespace, the project client and the launch context; the session is created at the first launch |
| `DELETE /v1/researchers/{researcher}/projects/{project}/launch` | Platform component | The session, its namespace and its redirect URI are removed |
| `POST /v1/researchers/{researcher}/projects/{project}/model-key` | Platform operator | A new worker key of the session's model route with the remaining budget, see [Model Route](#model-route) |
| `POST /v1/projects/{project}/client-secret` | Platform operator | A new secret of the project client, generated by Keycloak and written to the gateway Secrets of the members' sessions; the reply lists the rewritten sessions and those that could not be prepared, see [Recovery and Client Secret Rotation](#recovery-and-client-secret-rotation) |
| `GET /v1/launch-context` | Heartwood gateway | The launch context of the researcher and project that the token names |
| `POST /v1/projects/{project}/launch-context` | Platform service | The launch context identifier of the session of the researcher whose subject the JSON body names, `{"subject": ...}`, for the data service and the job service; HTTP 403 for a non-member and 404 without a session |
| `POST /v1/projects/{project}/job-quotes` | Job service | Price of a runtime profile and run time limit in seconds |
| `PUT /v1/projects/{project}/job-reservations/{operation}` | Job service | Hold the full price of the limit or refuse admission |
| `POST /v1/projects/{project}/job-reservations/{operation}/settlement` | Job service | Charge observed seconds and release the hold. Zero seconds releases an unused reservation. |
| `/mcp`, MCP over streamable HTTP | Heartwood gateway | The read-only tools `get_budgets` and `estimate_workload` |

A platform component sends a projected service account token with the audience `heartwood-control`.
The service verifies it with a TokenReview and accepts the service accounts in `control.serviceCallers`.
A platform operator sends the same kind of token, and the service accepts it for the rotation only from the service accounts in `control.operatorCallers`.
A platform service sends the same kind of token for a launch context, and the service accepts it only from the ServiceAccounts of the data service and the job service and from the service accounts in `control.contextCallers`.
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
Both tools refuse a `project_id` other than the token's project and a researcher without a session in the project, and `estimate_workload` refuses a prediction whose `project_context_id` is not the launch context of that session.
Every price applies to every project, because `SkuPriceEligibility` must be `{"IsGlobalScope": true}`.

### Budgets

Each project has one budget for each category of its work, `inference` and `compute`, with the amount that `projects[].budgets` allocates to it in the currency of the price sheet.
The service keeps the budgets in its database together with the reservations of running work and the spending of settled work.
A budget's available amount is its allocation minus its reservations and its spending.
The service sets the allocations to the chart values at each start, so a changed value changes the availability of the budget, and a budget that is no longer listed has nothing allocated.

A session reserves `control.inference.sessionReservation` of its project's inference budget at its first launch, or the available amount when less is left, and keeps the reservation until it is deleted.
A launch of a session without a reservation is refused with HTTP 409 when the budget has nothing available, before anything is provisioned, and the Hub refuses the start.
Each reservation is one database transaction that checks the available amount, so concurrent launches cannot reserve more than a budget holds.
Deleting a session settles its reservation: the service records what the session incurred, also when it exceeds the reservation, and releases the rest.

The MCP tool `get_budgets` takes `project_id` and returns `{"budgets": [...]}`, one `heartwood.budget-observation.v1` document for each budget of the project, with the allocated, reserved, spent and available amounts, the observation time and the launch context of the researcher's session.
Its `platform_record_ref` changes with every change of the budget.
Reading the budgets reserves nothing and admits no work.

### Project Clients

At a launch the service creates the Keycloak client `hw-proj-<project>` unless it exists: a confidential client with the authorization code flow and standard token exchange, without direct access grants and without a service account, and with the optional scopes `svc-control`, `svc-data` and `svc-jobs`.
It adds the redirect URI of the researcher's gateway, `https://<researcher>.<project>.<hub.projectDomain>/user/<researcher>/<project>/oauth_callback`, and serializes the updates of one client, because Keycloak replaces the whole client on an update.
Its Keycloak role `manage-clients` covers every client of the realm.

### Permitted Data

The reference platform is for synthetic and open data only.
For a platform-mounted interactive dataset, set `projects[].interactiveDataset` to its `id`, `version` (the SHA-256 digest of its verified content manifest), logical byte `size`, object count `objects`, and the platform mount account name `reader`.
Publish and verify the version under `datasets/<id>/<version>/` in the project bucket before granting it.
Preserve the version after retirement.
The bucket policy seals each admitted version against writes and deletes.
On upgrade the policy writer reads the project bucket policies, preserves their seals and withdraws reader grants for retired versions and removed projects.
The active reader can read objects and list its version prefix.
Install the Mountpoint S3 CSI driver before the chart, with driver authentication through that account and `mountpointPod.podLabels` set to `heartwood.platform/role: dataset-reader`, so that the store client and its credential stay outside the worker pod.
The chart admits these Mountpoint pods from the platform-managed `mount-s3` namespace to the store.
The store presents a certificate of the platform CA, and `mount-s3` starts with a cleared environment and reads only its image's trust bundle `/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem`, so CA environment variables do not reach it.
The chart therefore writes the platform CA to the ConfigMap `platform-ca` in `mount-s3`, and its MutatingAdmissionPolicy `heartwood-dataset-reader-ca` mounts that certificate read-only over the trust bundle of each labeled single-container Mountpoint pod at creation.
These pods trust only the platform CA, so a driver installation that labels its pods this way serves only the platform's dataset volumes.
A Mountpoint pod created before a CA rotation keeps the previous certificate until the driver recreates it.
When a project has a mounted dataset, the chart needs Kubernetes 1.36 or later, which serves the `admissionregistration.k8s.io/v1` MutatingAdmissionPolicy API, and an existing `mount-s3` namespace; the installation fails without either.

The launch API selects the project's configured version for members. An optional `dataset=<id>@<version>` query parameter requires that exact selection. A session receives the version at `/data` through a read-only claim. Reapplying a changed configuration restarts control, which removes sessions whose membership or version grant expired. The session namespace owns the static volume. Namespace deletion retires the claim and volume while the underlying objects remain in the project bucket. Projects without a mount grant use the catalog copy path. Batch tasks stage their declared inputs.

The chart loads the synthetic datasets in `files/datasets/`, and the cluster tests write further synthetic objects into the project buckets.
The platform is not set up for controlled, sensitive or identifiable data, and such data must not be loaded into it.

### Data Service

The data service serves the catalog of the projects' synthetic and open datasets.
Each entry of `projects[].datasets` names a dataset, its description, its creation time and its files, which the chart reads from `files/datasets/<dataset>/<file>`.
The chart computes the size and the SHA-256 checksum of each file, and the bootstrap Job `heartwood-data-bootstrap` writes the files into the project's bucket under `datasets/<dataset>/<file>`.
An object's DRS identifier is `<dataset>.<file>`, so a dataset identifier names one dataset on the whole platform.
The files in `files/datasets` are synthetic.
The bootstrap Job reads all dataset files from one ConfigMap, so together they may hold at most 768 KiB, and rendering fails above that.
Projects with datasets need `sessions.attemptStorageClass.provisioner`, whose StorageClass `heartwood-project-data` holds the project data volumes; rendering fails without it.

The service serves only HTTPS on port 8443, with a certificate that the platform CA signs for the names of its Service, like the control service.

| Request | Caller | Result |
|---|---|---|
| `GET /ga4gh/drs/v1/service-info` | Anyone who reaches the service | The GA4GH service information of the DRS 1.5 service |
| `GET /ga4gh/drs/v1/objects/{object_id}` | Heartwood gateway | The DRS object: name, size, SHA-256 checksum, media type, creation time and the access method `https` |
| `GET /ga4gh/drs/v1/objects/{object_id}/access/https` | Heartwood gateway | A URL that reads the object from versitygw |
| `POST /v1/volume-plans` | Control service | Every object of a project with its path in a project data volume, its checksum and a URL, and the digest of the volume's content |
| `/mcp`, MCP over streamable HTTP | Heartwood gateway | The read-only catalog tools `list_datasets`, `list_objects`, `resolve_object` and `get_operation` |

A Heartwood gateway sends a Keycloak access token that the project client obtained by token exchange for the audience `heartwood-data`, verified like the tokens of the control service.
The project comes from the token: an object of another project does not exist for the caller and receives HTTP 404, and a researcher whom `projects[].members` does not list receives HTTP 403.
The control service sends a projected service account token with the audience `heartwood-data`, which the data service verifies with a TokenReview, and names the researcher and the project, whose membership the data service checks.

A URL is a presigned S3 request of the data service's versitygw account `heartwood-data`, signed for one object and the method `GET`.
It stays valid for 300 seconds, which covers starting the copy pod, plus one second for each MiB of the object, up to one hour.
The account reads the objects of every project bucket through the bucket policies of [Bucket Policies](#bucket-policies) and can neither write nor list.
Its key lives in the Secret `versitygw-data`; the root account of versitygw in `versitygw-root` is used only by versitygw and the hook Jobs that create the buckets and accounts, write the objects and set the bucket policies.

The catalog tools follow Heartwood's operation binding for research MCP servers.
Each takes `project_id` and `operation_id`, requires an exchanged token for `heartwood-data` of a project member, refuses a `project_id` other than the token's project, and returns a `heartwood.platform-operation-observation.v1` that names the launch context of the researcher's session, which the service reads from the control service.
`list_datasets` returns up to ten of the project's datasets from an offset on, fewer when their encoded text would exceed 8 KiB, in the observation's `result`, each with its name, the first 500 characters of its description and its number of objects, and the offset of the next datasets.
`list_objects` returns up to ten objects of one dataset from an offset on, each with its DRS identifier, size, checksum, media type and its path below `/data`, where workers read the project data volume, and the offset of the next objects.
The answers stay far below the gateway's limit on a tool result, which counts the observation twice, as structured content and as text.
`operation_id` must be a UUID, as the gateway sends it.
`resolve_object` returns the DRS object of one identifier with its path, and denies an identifier that names no object of the project, as `list_objects` denies a dataset the project does not have.
The service keeps each observation by researcher, project and operation identifier in a SQLite database on its volume: a repeated identifier with the same request returns the kept observation, and one with another request is refused.
`get_operation` reports a kept operation and records an identifier it has not seen as failed, so that identifier is refused afterwards.

The control service writes the catalog into each hosted gateway's deployment manifest as an `[[mcp_destinations]]` entry with the service identifier `heartwood-data`, the three selectable tools, `get_operation` as observation tool, a result limit of 32 KiB, the platform CA and the token exchange for `svc-data`.
A researcher adds it as a research service at `https://heartwood-data.<release namespace>.svc.cluster.local:8443/mcp`, reviews and selects its tools, and enables it; each call is then an outside operation with its own review and result release.
The control service answers the data service's request for a launch context with the context's identifier only.

### Project Data Volumes

At each launch the control service fills the session's project data volume with the objects of the researcher's project:

1. It asks the data service for the access plan of the researcher and project.
2. It applies the network policy `hw-project-data-copy-egress`, which lets pods with the label `heartwood.platform/role=project-data-copy` reach DNS and versitygw, and creates the volume claim `hw-project-data-<digest>` with the StorageClass `heartwood-project-data`, named after the first 16 hexadecimal digits of the content digest.
3. It runs a copy Job in the session namespace with the platform services image, the platform CA certificate and the claim, and writes the plan into a Secret that the Job owns, so deleting the Job deletes the plan.
   The copy pod runs under the restricted Pod Security Standard without a service account token and without a credential of versitygw.
   It downloads each object over HTTPS, verifies its size and checksum, and writes it to `<dataset>/<file>`, readable by every user.
4. It deletes the Job and the Secret when the Job ends; a Job that a restart of the control service left behind is deleted with its Secret ten minutes after it ended.
   A failed Job is run again with a new plan and new URLs, up to three times, and then the launch fails.
5. After a successful copy it adds the labels with which the OpenShell driver attaches the claim to the sandboxes of the workspace `research`, and deletes the session's claims of earlier content.

A launch with an unchanged catalog reuses the filled claim.
The claim carries the labels `heartwood.platform/session` and `heartwood.platform/project` and the annotation `heartwood.platform/content-sha256`.
The reply to the launch and `launch.json` name it in `project_data` with its UID, its content digest, the mount path `/data` and the number of objects.
The gateway's deployment manifest declares it in `[worker.project_data]` with the project of the launch context, so the gateway attaches it read-only to each worker and admission checks it; the default worker policy in `hub.worker.policy` lists `/data` read-only.
A project without datasets has no project data volume.
Until they expire, the URLs of a plan are readable by the ServiceAccount `openshell-certgen` of the OpenShell certificate Job, which reads Secrets of the session namespace, and by cluster administrators.
The admission policy `heartwood-project-data-copy` keeps the label `heartwood.platform/role=project-data-copy`, which admits a pod to versitygw, to the pods that the Job controller creates for the control service's copy Jobs.

### Job Service

The job service, `python -m heartwood_platform.jobs`, runs batch tasks on the TES executor of each project and serves MCP tools that follow the operation profile of the [platform contract](../documentation/operate/platform-contract.md#platform-job-operations).
A Heartwood gateway calls them with a researcher's token exchanged for the audience `heartwood-jobs`, verified like the control service's tokens.
The project comes from the token's authorized party, and the service asks the control service for the launch context of the researcher's session in that project, with its own projected service account token.
A non-member, a member without a session, and a `project_id` other than the token's project are refused, and every observation carries the session's `project_context_id`.

| Tool | Annotations | Result |
|---|---|---|
| `describe_operations` | read-only | The operations, the permission each requires, and the GPU capability: available only to projects with a GPU entitlement |
| `propose_task` | read-only | A `heartwood.platform-operation-proposal.v1` document for `create_task` arguments: limits of CPU cores, memory, disk, duration and GPUs, the runtime profile of the price sheet, the data scope and the results location; nothing is submitted |
| `create_task` | side effects, idempotent | Submits one task per operation identifier and returns its observation |
| `get_task` | read-only | The observation of an operation; an identifier that the service never received is reported as failed and refused afterwards |
| `cancel_task` | side effects, destructive, idempotent | Cancels the queued or running task of `target_operation_id`, as an operation of its own that reports the TES state it observed |

A task runs one container image, which must be one of the task images that the platform lists by digest.
Its inputs are project objects by DRS identifier `<dataset>.<file>`, read below `/inputs`; its outputs are files below `/outputs`, written to `results/<operation>/<name>` in the project's bucket.
An identifier `<dataset>.<file>` names the object `datasets/<dataset>/<file>` of the project's bucket, where the data service stores the catalog's objects.
The service holds one object store account for each project and gives the executor only presigned URLs for single objects.
It checks that each input exists before it submits the task.
An input URL expires 300 seconds after the start deadline, and an output URL 300 seconds after the start deadline plus the task's run time limit.
Task tags may not start with an underscore, which Funnel reads as instructions, or with `heartwood`; the service tags each task with `heartwood-operation` and its operation identifier.

An operation belongs to the researcher who sent it: another member of the project can neither observe nor cancel it.
A repeated operation identifier with the same arguments returns the existing task, and other arguments or another researcher are a conflict.
A request outside the project's entitlements or limits is recorded and reported as denied, and no task is created.
The limits include the GPUs of one task and the number of tasks that may wait or run at once.
A request with an image that the platform does not run, or with an input that the project does not hold, is denied in the same way.
An operation's observations carry the launch context that it was received in.
The platform does not select GPU types, so a request that names one is denied.
The service records each submission in its SQLite database before it reserves the budget and calls the executor.
It runs as one replica and serializes the operations of each project.
When the executor's reply is lost or is a server error, a repetition or the status finds the task by its operation tag and a creation time no earlier than 60 seconds before the record; a task that is not found within 120 seconds is reported as failed.
A client error from the executor is reported as failed at once.
At start and then every `reconcile_seconds`, 60 by default, the service lists each project's tasks that may still run and cancels those that no operation recorded, such as a task created around the service with the executor's credential, and those whose operation has ended.
A proposal whose description or data scope is longer than 8192 characters is refused, because the researcher reviews it in full.

| TES state | Status |
|---|---|
| `QUEUED`, `INITIALIZING` | queued |
| `RUNNING`, `PAUSED`, `CANCELING` | running |
| `COMPLETE` | completed |
| `EXECUTOR_ERROR`, `SYSTEM_ERROR`, `PREEMPTED` | failed |
| `CANCELED` | cancelled |
| `UNKNOWN` | unknown |

A reported state never moves back to an earlier one.
The service cancels a task that does not start within the start deadline or runs longer than its run time limit and reports it as failed with the reason.
A failure carries a fixed explanation: an exit code, a task that ran out of memory, a task killed with exit code 137 without an out-of-memory report, as when its pod is deleted or evicted, an input that could not be downloaded, an output that was not written, an image that could not be pulled, or a lost worker.
System logs and task output are not passed on, because system logs contain presigned URLs.

The service reserves a project's compute budget through the `JobBudgets` interface, for the task's hard limits at submission, and settles the reservation by its holder, `job/<project>/<operation>`, with the task's run time when the task ends.
The job service authenticates with its service account and reserves the full compute price of the task's run time limit before it submits the task to the executor.
Settlement sums the durations of individual TES attempts, excluding gaps between retries, and rounds the total up once to whole seconds.
The service saves attempt timestamps on each observation. A running observation supplies a missing start, and a later executor timestamp supersedes it.
A queued cancellation without a start incurs zero. If the task disappears, the service uses the time it observed the loss to close the last open attempt.
The ledger charges those seconds at the unit price and seconds per pricing unit saved at admission, rounds the charge up to six decimal places, and releases the unused reservation.
The ledger records its denomination, including whether it is a currency or allocation unit. Restarting it requires a price sheet in that same denomination.
Repeated requests for the same operation preserve its original reservation or settlement.

An incompatible jobs database or a ledger without a recorded denomination stops startup.
To retain access to existing work, use the compatible service version to finish active jobs and settle outstanding reservations.
Before starting fresh state, stop the services and preserve the jobs and control data volumes together, including their SQLite journals. Use separate volumes for a fresh installation.

The control service writes the job service into each hosted gateway's deployment manifest as an `[[mcp_destinations]]` entry with the service identifier `heartwood-jobs`, the selectable tools `create_task` and `cancel_task`, `get_task` as observation tool, the operation profile of `describe_operations`, `propose_task` and `cancel_task`, a call time limit of 60 seconds, the platform CA and the token exchange for `svc-jobs`.
A researcher adds it as a research service at `https://heartwood-jobs.<release namespace>.svc.cluster.local:8443/mcp` and enables its tools; the gateway then shows each submission and cancellation of a worker as a proposal from `propose_task`, submits it after the researcher approves it, observes it with `get_task`, and releases its result to the worker after a further review.

The executor is reached only through the `TesExecutor` interface, with five operations: service information, create, get, list and cancel.
The deployed implementation calls a TES 1.1 HTTP API over HTTPS with a CA file and a Basic credential, and sends the colon of `:cancel` encoded.

### Job Executors

The chart value `jobs.executor` selects the executor of the job service; `funnel`, the only value, installs one Funnel server for each project, from `images.funnel`, in the namespace `hw-jobs-<project>`.
The network policies and the admission policy select pods by the role labels `heartwood.platform/role` `funnel-server`, `funnel-worker` and `funnel-executor`.

| Object in `hw-jobs-<project>` | Content |
|---|---|
| Namespace | Pod Security `restricted`, the label `heartwood.platform/jobs` |
| ResourceQuota `jobs` | `jobs.quota` |
| Deployment `funnel-server` | UID 65532, read-only root filesystem, BoltDB on the claim `funnel-db`, TLS on both ports with the certificate `funnel-server-tls` that the platform CA signs for the Service's names |
| Secret `funnel-server-config` | The server's configuration, with the job service's password as the only Basic user, `TaskAccess: OwnerOrAdmin`, `Server.WorkerCredentialKeyFile` and only the HTTP storage backend |
| Secret `funnel-worker-key` | The key from which the server derives each task's worker credential |
| ConfigMap `funnel-worker-config` | The worker's configuration: the server's gRPC address, the platform CA and the credential file that the task's Secret provides; it holds no credential. Workers move the files of their tasks with presigned URLs over HTTPS and trust the platform CA alone (`SSL_CERT_FILE`) |
| ServiceAccounts | `funnel-server` and `funnel-worker` with a token, `funnel-executor` without |
| Role `funnel-server` | `jobs` `create, list, get, delete`; `persistentvolumeclaims` `create`; `pods` `get, list`; `secrets` `create, delete` |
| Role `funnel-worker` | `jobs` `create`; `pods` `watch`; `pods/log` `get` |
| NetworkPolicies | Default deny of ingress and egress; the server receives port 9090 from workers and port 8000 from the job service, and reaches DNS and the API server; workers reach the server's port 9090, the object store, DNS and the API server; executor pods reach nothing |

For each task, the server creates the worker Job from `files/funnel/worker-job.yaml`, the workspace claim `<task>-workspace` and the Secret `funnel-worker-credential-<task>`, which only that worker Job mounts.
The worker creates the executor Job from `files/funnel/executor-job.yaml`: the task image as the only container, as `funnel-executor` without a token, with `hostUsers: false`, the task's CPU and memory as requests and limits, and the workspace claim.
Funnel sets the executor container's `nvidia.com/gpu` request and limit to the GPU count admitted by the job service.
For a positive count in a GPU-entitled project, the executor template adds the toleration for `nvidia.com/gpu=present:NoSchedule`.
CPU tasks receive no GPU toleration.
Worker and executor run as root in their own user namespaces, without capabilities and without privilege escalation; the server runs as a non-root user.

The ValidatingAdmissionPolicy `heartwood-funnel-worker-jobs` applies to the Jobs that a `funnel-worker` ServiceAccount creates in a jobs namespace.
A worker's token is bound to its pod, whose name is its task identifier with a suffix, and a Job of that worker must:

- carry exactly the executor labels of that task, apart from the selector labels that the API server adds to its pod template, a name `<task>-<index>`, and no selector of its own;
- run as `funnel-executor` without a ServiceAccount token, in a user namespace;
- mount only the claim `<task>-workspace` of that task, and no other volume;
- have one container, no init or ephemeral containers, no pull secrets, no environment from Secrets or ConfigMaps, and no node, scheduling, runtime class, proc mount or host name of its own;
- tolerate nothing, except that a Job whose container requests GPUs in the namespace of a project with the GPU entitlement, labeled `heartwood.platform/gpu: "true"`, may tolerate exactly the GPU nodes' taint `nvidia.com/gpu=present:NoSchedule`, with the operator `Exists`, or `Equal` and the value `present`.

A worker cannot change a Job, and each Job runs one pod once, without a pod failure policy and without a restart policy or restart rules of its container.
The quota bounds the number of Jobs in the namespace and allows GPUs only to a project with the GPU entitlement.
A worker can read the logs of every pod in its namespace, including the executor logs of other tasks of the same project.
A session namespace that the control service creates may not carry the jobs label.
The job service holds the Secret `heartwood-jobs-executors` with its password at each project's server, and `heartwood-jobs-store` with its account `hw-jobs-<project>` at the object store.
The hook Job `heartwood-jobs-store` creates these accounts, and each account reads its project bucket's objects and writes below `results/` through the bucket policies of [Bucket Policies](#bucket-policies).
The job service runs as one replica with its records on the claim `data-heartwood-jobs-0` and its certificate `heartwood-jobs-tls`; gateways in the sessions namespace reach it on port 8443.
It reaches Keycloak for the keys of researchers' tokens, the control service with a projected token of the audience `heartwood-control`, the project servers, and the object store.

### Bucket Policies

versitygw keeps one policy for each bucket, and writing a policy replaces the previous one.
So one hook Job, `heartwood-bucket-policies`, writes the policy of each project bucket with the statements of every account that reaches it, after the hooks that create the accounts.
In each bucket `hw-project-<project>`, the data service's account reads every object, and the project's job account `hw-jobs-<project>` reads every object and writes below `results/`.
The ConfigMap `heartwood-bucket-policies` holds these grants, and `heartwood_platform.common.bucket_policies` writes them with the root account over HTTPS.
No other Job writes a bucket policy.

### Session Namespaces

At each launch the control service brings the namespace `hw-s-<session>` to the same state, and every step can be repeated:

1. It creates the namespace with the labels `heartwood.platform/session` and `pod-security.kubernetes.io/enforce=restricted`, or reuses a namespace that carries them and `pod-security.kubernetes.io/enforce-version=latest`.
2. It binds `heartwood-session-control` to itself there, and applies the ResourceQuota and LimitRange from `sessions.quota`, `sessions.containerDefaults` and `sessions.containerDefaultRequests`.
   It also applies the ServiceAccount `hw-staging` without a mounted token, for the staging pods of the Heartwood gateway, and the ConfigMap `platform-ca` with the platform CA certificate.
3. It applies the network policies: no ingress and no egress by default; ingress to the OpenShell gateway on port 8080 from the supervisors of its sandboxes, from pods in `heartwood-sessions` that carry the session label and from the control service; egress of the OpenShell gateway and its certificate Job to DNS and to the Kubernetes API server.
4. It creates the Secret `openshell-kek` with a new 32-byte key, unless the session has one. OpenShell encrypts the provider credentials of the session with it.
5. It binds the other fixed ClusterRoles with RoleBindings and applies the OpenShell objects with server-side apply, with the namespace and `images.openshellGateway` in place of the placeholders, and `images.openshellRepository`, when set, in place of the repository of the supervisor and sandbox runtime images.
6. It sets the subjects of `heartwood-openshell-node-reader` to the OpenShell ServiceAccounts of all sessions in the registry whose removal has not begun.
7. It creates the ServiceAccount `hw-gw-<session>` in `heartwood-sessions` and waits until the OpenShell gateway is ready.
8. It gives the session its keys of the model route, see [Model Route](#model-route), and replaces the Secret `hw-gw-<session>`.

The Secret `hw-gw-<session>` holds the gateway's deployment manifest `deployment.toml`, a `kubeconfig` for the session namespace that authenticates with the pod's ServiceAccount token, the worker policy `worker-policy.json`, the launch context `launch-context.json`, `launch.json`, the secret of the project client, the OpenShell client certificate, key and CA certificate, the platform CA certificate `platform-ca.crt`, the deployment's actor key `actor-key`, the session's discovery key `model-discovery-key`, and the gateway's serving key and certificate `gateway-tls.key` and `gateway-tls.crt`.
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
The Hub trusts the platform CA alone for Keycloak and the control service, without the certificate directory that libcurl reads by default, and its user name is the researcher identifier of the subject of the researcher's sign-in.
Every account of the realm may sign in; the control service decides which projects a researcher may start.

The proxy serves researchers only over HTTPS, on port 443 of the Service `proxy-public`, with the certificate in the Secret `proxy-tls`.
The chart signs that certificate with the platform CA for the names of the Services `proxy-public` and `proxy-api`, in the same four forms as Keycloak's, and for the host of `hub.publicOrigin`, which must be an `https` origin with a DNS host name.
Configure wildcard DNS for `*.<project>.<hub.projectDomain>` for each registered project to resolve to the public proxy on port 443.
The proxy certificate includes each of these wildcard names, and the Hub routes each named server on its researcher and project host.
Use a dedicated project DNS zone with no shared cookie domain with the Hub or identity provider.
The Hub uses host-prefixed authentication cookies.
A change of these names or of the platform CA replaces the certificate at the next upgrade.
The proxy reads its certificate when it starts, so restart it after such a change with `kubectl rollout restart deployment/proxy --namespace heartwood-platform`.
The kind cluster configures CoreDNS for the synthetic zone `projects.heartwood.test`.
When changing a project domain or moving from shared paths, withdraw browser access during the change.
Keep the session volumes, stop the old gateway processes, and remove obsolete redirect URIs from the project clients.
Restart the control service, Hub and proxy with the new configuration before reopening access.
Relaunching a named server registers its new callback and requires a new gateway sign-in.
The Service has no HTTP port, and no connection reaches the proxy's HTTP port, which would only redirect to HTTPS.
The proxy serves its API on port 8001 over HTTPS with the same certificate, and the Hub calls it at `proxy-api` trusting the platform CA alone.
Clients of the proxy, such as the cluster tests, verify it against the platform CA, and a researcher's browser opens it only when it trusts the platform CA.
The proxy forwards requests to the Hub on port 8081 and to the gateways on port 8888 over TLS.
It verifies both against the platform CA alone and presents its own certificate as a client certificate, which `proxy-tls` carries with the CA certificate `ca.crt`.
The proxy forwards the researcher's `Host` header unchanged, and a module that `NODE_OPTIONS` preloads makes it verify each certificate against the host of the route's target.

The Hub serves port 8081, its pages and its API, only over TLS with the certificate in the Secret `hub-tls`, which the chart signs with the platform CA for the names of the Service `hub` and for `localhost`.
`files/hub/heartwood_hub.py` passes the certificate, its key and the platform CA to JupyterHub through the key, certificate and CA settings of its internal TLS.
JupyterHub then requires a client certificate of the platform CA on that port; the configuration replaces JupyterHub's TLS context, which would also trust the certificate authorities of the Hub image's system store.
JupyterHub's full internal TLS mode stays off: it creates certificate authorities of its own, and KubeSpawner then creates a Secret and a Service for each server.
The idle culler in the Hub's pod reaches the Hub at `localhost` with the Hub's certificate.
kubelet cannot present a client certificate, so the Hub has no liveness or readiness probe; the start hook of its container waits up to 300 seconds until the Hub answers over TLS, so the pod becomes ready only then.
kubelet does not restart a Hub that stops answering after it started.

Each gateway serves HTTPS on port 8888 with its own certificate.
A gateway pod has the host name `hw-gw-<session>` under the headless Service `hw-gateways` in `heartwood-sessions`, and the proxy and the Hub reach it as `hw-gw-<session>.hw-gateways.heartwood-sessions.svc.cluster.local`.
At each start, an init container of the control service creates an issuing CA that the platform CA signs, valid for at most 825 days and no longer than the platform CA.
Its name constraints permit only DNS names under `hw-gateways.heartwood-sessions.svc` and `hw-gateways.heartwood-sessions.svc.cluster.local` and no IP address, and its extended key usage permits only server certificates, so TLS clients reject a certificate that it signs for another name, and the Hub rejects one that it signs as a client certificate.
Only that init container mounts the key of the platform CA; it writes the issuing CA into a memory volume of the control service's pod, and a restart of the control service creates a new one.
At each launch of a session, the control service signs a new key and certificate for the gateway's two names, valid for 30 days, and writes them with the issuing CA's certificate into the session's Secret, so a relaunch replaces them.
The idle culler stops a server 27 days after its start, whether or not it is used, and its next start receives a new certificate.

Each project of a researcher is one named server whose name is the project.
The spawn form offers a server only the project of its name, after the Hub asks the control service for the researcher's projects with a projected token of the audience `heartwood-control`.
Before each start the Hub asks the control service to prepare the session, and it refuses the start when the researcher is not a member of the project.
It then starts the gateway pod in `heartwood-sessions` with the ServiceAccount `hw-gw-<session>`, the session label, the volume claim `hw-home-<session>` and the hosted gateway image.
The init container runs `heartwood-hosted-configure`, which copies the Secret `hw-gw-<session>` into a memory volume with owner-only files, registers the session's OpenShell gateway with its client certificate, and creates the research and state directories on the claim.
The gateway mounts the copies read-only at `/etc/heartwood/deployment` and the registration at `/etc/heartwood/native` from the memory volume, and the research and state directories at `/research` and `/var/lib/heartwood` from the claim; `templates/_hub.tpl` lists these mounts.
The gateway serves `heartwood gateway serve` with `identity-proxy` ingress under `/user/<researcher>/<project>`, and accepts proxied requests only from `hub.trustedProxySources`.
The gateway's deployment manifest binds the subject itself, which the gateway compares with the subject of each token.
The gateway records an actor derived from the issuer and the subject under the deployment's actor key, as [Researcher Identity](../documentation/operate/security.md#researcher-identity) describes.
Helm generates that key at the first installation into the Secret `heartwood-actor-key`, keeps it on upgrades and leaves it in place on `helm uninstall`; a render without cluster access, such as `helm template`, generates a new value.
The control service reads the key when it starts and copies it into each gateway's Secret as `actor-key`.
Back up the Secret with the deployment's other credentials: a lost or replaced key gives every researcher new actors that nothing links to earlier records.
After replacing the key, restart the control service; a gateway keeps recording under the earlier key until its server starts again, which writes the new key into its Secret.

The control service writes the gateway's deployment manifest into the Secret: the identity of the researcher and the project client, the worker on the Kubernetes driver in the session namespace, the model route, and the control service as the `[planning]` service.
`hub.worker` sets the worker image and its sandbox policy, and `hub.modelRoute` the model route of the worker, see [Model Route](#model-route).
The OpenShell gateway admits a worker with the digest of its effective policy, which covers the endpoint of the route's provider and the identity of the provider in the session's OpenShell gateway.
At each start of the gateway pod, the init container runs `heartwood-hosted-policy`: it creates a sandbox with the worker image, the policy and the provider, writes the digest of its admission into the manifest's `[worker]` table and removes the sandbox, so the gateway admits only workers of that policy.
The digest is learned from the session's own OpenShell gateway, which is trusted for the session: the check then shows that every worker runs with the effective policy of the probe, and the policy file is checked against the manifest's `policy_file_sha256` first.

The idle culler stops a server after `jupyterhub.cull.timeout` seconds without traffic through the proxy.
The server's volume claim, the session namespace and the OpenShell gateway stay, and the next start runs the same session; the researcher signs in again, because tokens live in the gateway's memory only.
Deleting a named server deletes the session at the control service and the server's volume claim.

The Role `heartwood-hub-spawner` in `heartwood-sessions` grants the Hub what KubeSpawner needs: pods `create`, `delete`, `list` and `watch`, volume claims `create`, `delete` and `get`, and events `list` and `watch`.
It grants no access to Secrets or Services; KubeSpawner touches Secrets and Services only with JupyterHub's internal TLS mode or per-server Services, which the chart leaves off.
The Hub remains a trusted platform component: it can start the gateway of any session with that session's ServiceAccount and Secret, and delete pods and volume claims in `heartwood-sessions`.

`heartwood-hub-pods` applies to the Hub's ServiceAccount in `heartwood-sessions`:

- A pod carries its session label and the role label `hosted-gateway`, uses the ServiceAccount `hw-gw-<session>` of that session, and has the host name `hw-gw-<session>` under the Service `hw-gateways`.
- A pod runs one init container and one container of `images.hostedGateway`; the init container runs only `heartwood-hosted-configure`, and the container only `heartwood gateway serve` with the fixed arguments above.
- A pod carries only the labels of a JupyterHub server and its session labels, and only annotations under `hub.jupyter.org/`.
- A pod mounts only its session's Secret, a memory volume, a temporary volume, the volume claim `hw-home-<session>` and the standard ServiceAccount token volume, and each container mounts exactly the volumes, paths and subdirectories of the layout above, so only the init container mounts the Secret and the copies stay in the memory volume.
- A pod sets only environment variables of the names that JupyterHub and KubeSpawner use and `HOME`, with literal values, has no lifecycle hook, no exec probe, no probe of another host and the default termination message, and resolves names through the cluster's DNS only.
- A pod runs as user `10001` without host access, with the default seccomp profile, a read-only root file system and no capabilities.
- A volume claim is named `hw-home-<session>` and names no data source, volume or selector.

`heartwood-sessions` enforces the restricted Pod Security Standard.

### Recovery and Client Secret Rotation

A session's state lives on the server's volume claim and in the control service's registry, so a gateway pod that fails, a Hub that restarts and a control service that restarts leave it intact.
A Hub restart keeps running gateways and their routes: the Hub leaves its servers running when it stops, and the proxy runs in its own pod.
After a failed gateway pod, the researcher starts the server again and signs in again.

The client secret of a project is rotated through the control service by a platform operator, whose ServiceAccount `heartwood-operator` has no Kubernetes rights.
`control.operatorCallers` lists the accounts that may rotate; no other caller may.
`POST /v1/projects/<project>/client-secret` replaces the secret in Keycloak, which then refuses the previous one, and writes the new secret into the Secret `hw-gw-<session>` of every session of a current member of the project.
It answers with the rewritten sessions and with the sessions it could not prepare, whose Secrets keep the previous secret until the next start of their server writes them again.
A project without a client, because nobody has launched in it yet, has nothing to rotate and receives HTTP 409.
A former member's session keeps the previous secret, which Keycloak refuses.
A running gateway keeps the tokens it exchanged before the rotation until they expire; afterwards its exchanges fail, and the researcher's sign-in ends when the gateway can no longer refresh it, until its server starts again.
Deleting the gateway pods of the listed sessions ends them at once.

```bash
token="$(kubectl --namespace heartwood-platform create token heartwood-operator --audience heartwood-control)"
kubectl --namespace heartwood-platform get configmap platform-ca --output 'jsonpath={.data.ca\.crt}' > platform-ca.crt
kubectl --namespace heartwood-platform port-forward statefulset/heartwood-control 8443:8443 &
sleep 3
curl --cacert platform-ca.crt --resolve heartwood-control:8443:127.0.0.1 --request POST \
  --header @<(printf 'Authorization: Bearer %s' "${token}") https://heartwood-control:8443/v1/projects/demo/client-secret
kill %1
```

The control service logs which operator account rotated which project.

Attempt keys are generated for each worker attempt, so a new attempt never reuses the key of an earlier one.

### Model Route

Workers reach models only through LiteLLM, which the chart installs with `images.litellm`, the non-root image of the LiteLLM release, pinned by digest.
LiteLLM serves only HTTPS on port 4000, with a certificate that the platform CA signs for the names of its Service, and keeps its keys in its own PostgreSQL database.
It runs as one replica, because it counts the spending of keys in memory before it records it.
Its administration interface is off, and only the control service holds its master key, which Helm generates into the Secret `litellm`.

`inference.models` lists the route's models and has no default.
Each model's `name` is the model name that LiteLLM offers and the runtime profile of `syntheticPrices` that prices its input and output tokens; LiteLLM records the cost of each request from these prices.
`litellmParams` are the model's LiteLLM parameters, and `apiKeySecret` names a Secret key that LiteLLM sends to the model as its API key.
`maxInputTokens` and `maxOutputTokens` are the largest input and output of one request that the model accepts, and `supportsToolCalls` declares tool support to the gateways.
LiteLLM does not enforce these limits, so they must not be lower than the limits the model itself enforces.
LiteLLM trusts the platform CA alone for the models it calls.

A hosted model, such as one on Vertex AI or Amazon Bedrock, is a model of the route with the LiteLLM parameters of its provider, the provider's credentials from a Secret through `inference.environment`, and `inference.trustPublicRoots`, with which LiteLLM also trusts the public roots of its image:

```yaml
inference:
  trustPublicRoots: true
  environment:
    - name: VERTEX_CREDENTIALS
      secretKeyRef: {name: vertex-credentials, key: credentials.json}
    - name: AWS_ACCESS_KEY_ID
      secretKeyRef: {name: bedrock-credentials, key: access-key-id}
    - name: AWS_SECRET_ACCESS_KEY
      secretKeyRef: {name: bedrock-credentials, key: secret-access-key}
  models:
    - name: hosted-gemini
      maxInputTokens: 131072
      maxOutputTokens: 8192
      supportsToolCalls: true
      litellmParams:
        model: vertex_ai/gemini-2.5-pro
        vertex_project: example-project
        vertex_location: us-central1
        vertex_credentials: os.environ/VERTEX_CREDENTIALS
    - name: hosted-claude
      maxInputTokens: 200000
      maxOutputTokens: 8192
      supportsToolCalls: true
      litellmParams:
        model: bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0
        aws_region_name: us-east-1
```

Each name needs input and output token prices in `syntheticPrices`, and LiteLLM needs egress to the provider's endpoints.
The models of a deployment and their data handling are the operator's choice; the route records their declared data handling in `hub.modelRoute.dataHandling`.

At its first launch a session gets one virtual key for its workers.
The key lists the route's models and only the routes `/v1/chat/completions` and `/v1/models`, so it cannot read other keys, the models' addresses or their health.
Its budget is the session's reservation minus the largest cost of one request to any model of the route, because LiteLLM checks a key's spending before each request and the last admitted request can end above the budget.
The key allows one request at a time, so only one request can be admitted below the budget and end above it.
A session whose project has no more than that cost available is refused.
The control service creates the provider `hub.modelRoute.provider` in the workspace `research` of the session's OpenShell gateway with the key, through the pinned OpenShell command-line tool in the platform services image, and keeps only the key's SHA-256 hash.
The provider's profile allows the key only in `POST` requests to LiteLLM's chat completions endpoint from `hub.modelRoute.binaries` of the worker image; a worker that attaches the provider sees a placeholder in `hub.modelRoute.placeholderEnv`.
OpenShell identifies a program by the path that its links resolve to, so `hub.modelRoute.binaries` names the worker image's interpreter by that path, which depends on the architecture; `deploy/qualify-kubernetes-worker.py images` reports it as `worker_interpreter`, and the [Kubernetes Qualification workflow](../.github/workflows/kubernetes-qualification.yml) installs the chart with it.
The value has no default, and the chart refuses to render without it, or with the stand-in program `/agnhost` of `values-kind.yaml` together with another `hub.worker.image`.
Each launch also gives the session a new discovery key, which may only list the route's models; the previous key stays valid for a gateway started before the launch, and the key before it is deleted.
The gateway lists models with it through the model connection `hub.modelRoute.profileId` of its manifest, trusting the platform CA alone, and the manifest declares each model's limits and tool support.
At a later launch the worker key keeps its budget and gets the route's current models, so a model removed from `inference.models` is refused from then on; a session whose OpenShell gateway lost the provider gets a rotated key.
Only the gateway's answer that the provider is not found counts as a lost provider.
While the tool cannot connect to the session's OpenShell gateway, as right after the gateway starts or restarts, the control service repeats the request with pauses that grow from half a second to eight seconds, for at most 60 seconds; a gateway it cannot reach in that time, or any other failure of the lookup, fails the launch with HTTP 502 and leaves the session's key unchanged.

LiteLLM refuses a request beyond a key's budget with HTTP 422 and the error type `budget_exceeded`, and a model outside the key's list with HTTP 403 and the error type `key_model_access_denied`.

Deleting a session settles its key: the control service sets the key's budget to its recorded spending, which blocks it, waits `inference.settleWaitSeconds` for LiteLLM to record the spending of the last requests, reads the spending until it stops changing, records it with the spending of earlier keys of the session as what the session incurred, and deletes the keys.
LiteLLM writes the spending every `inference.spendWriteSeconds` plus up to five seconds, and `inference.settleWaitSeconds` must exceed that.
The same interval drives LiteLLM's other periodic database jobs, so a low value is meant for test clusters.
The recorded amount can exceed the key's budget by up to one request.
When the spending does not stop changing, the deletion fails and is repeated.
A session whose deletion settled its key and then failed is not launched again until the deletion completes.

A platform operator rotates the worker key of a session with `POST /v1/researchers/{researcher}/projects/{project}/model-key`, accepted only from `control.operatorCallers`.
The control service measures the old key's spending as at a deletion, creates a key with the remaining budget, replaces the provider's credential with it, deletes the old key and rewrites the gateway Secret.
A worker attached to the old key fails at its next request, and the next worker uses the new key.

### Local Model

`inference.localModel` serves a model on the cluster's CPUs with `llama-server` from `images.modelRuntime`, the `runtime-image` stage of `images/Dockerfile`, which has no default.
The image holds no weights: an init container downloads `inference.localModel.model.url` over HTTPS at each start, up to `inference.localModel.model.maxBytes`, and checks its SHA-256 digest against `inference.localModel.model.sha256`.
`llama-server` serves HTTPS on port 8443 with a certificate that the platform CA signs for the names of its Service, and LiteLLM verifies it against the platform CA, so a server without that certificate receives no request.
It requires the API key that Helm generates into the Secret `llama-server`, read with `--api-key-file`, and only LiteLLM receives the key; it runs with `--no-webui`.
Its health and model list answer without the key, so the network policy admits only LiteLLM.
The model is a model of the route, listed in `inference.models` with the name `inference.localModel.alias`, `api_base` `https://llama-server.<namespace>.svc.cluster.local:8443/v1` and `apiKeySecret` `{name: llama-server, key: api-key}`.
When the runtime is not running, LiteLLM answers the model's requests with an error.

`values-kind.yaml` serves Stories 260K, the small test model that `images/generic/local-runtime/models/stories260k.toml` pins, with a context of 4096 tokens.
It exists to test the route and is not suited to research tasks.

### Network Policies

The namespaces `heartwood-platform` and `heartwood-sessions` deny ingress by default, and each namespace `hw-jobs-<project>` denies ingress and egress apart from the allowances in [Job Executors](#job-executors).
PostgreSQL accepts connections from Keycloak and LiteLLM.
Keycloak accepts connections from `heartwood-sessions`, from the Hub, from the control service, from the job service and from its bootstrap Job.
The control service accepts connections from `heartwood-sessions`, from the Hub and from the job service.
LiteLLM accepts connections on port 4000 from the control service, from the supervisors of session sandboxes and from the gateway pods in `heartwood-sessions`, and the synthetic model and `llama-server` accept connections from LiteLLM.
versitygw accepts connections from its bootstrap Job, from the hook Jobs `heartwood-jobs-store` and `heartwood-bucket-policies`, from the data bootstrap Job, from the job service, from Funnel workers in namespaces with the label `heartwood.platform/jobs` and from pods with the label `heartwood.platform/role=project-data-copy` in session namespaces.
Keycloak also accepts connections from the data service.
The data service accepts connections from `heartwood-sessions` and from the control service, and the control service also accepts connections from the data service.
The policy `heartwood-jobs` lets the job service accept connections on port 8443 from `heartwood-sessions` only.
The proxy accepts researchers' HTTPS connections on port 8443 from anywhere and configuration from the Hub on port 8001, and the Hub accepts TLS connections from the proxy.
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

`heartwood-staging-names` applies to the pods and Sandbox resources that a ServiceAccount creates in a namespace that carries the label `heartwood.platform/session`:
a pod or Sandbox resource whose name begins with `hw-staging`, also when the name is generated, is admitted only from a ServiceAccount `hw-gw-*` in `heartwood-sessions`.
A Heartwood gateway may open exec sessions into pods named `hw-staging-*`, so no other component, such as the OpenShell gateway or the Agent Sandbox controller for an OpenShell workspace of that name, creates a pod under such a name.
Requests of users other than ServiceAccounts, such as a cluster administrator, are outside the policy, so controllers must create pods with their own ServiceAccount credentials, as `kube-controller-manager --use-service-account-credentials` does on kind and kubeadm clusters.

`sessions.stagingImages` lists the worker images of the sessions' deployment manifests; during a change of the worker image it lists the current and the next image, and an empty list denies every pod.

`heartwood-project-data-copy` applies to pods and Jobs in session namespaces: only the control service creates Jobs named `hw-project-data-copy-*` that carry the label `heartwood.platform/role=project-data-copy`, only the Job controller creates pods with it for such a Job, no update adds it, and an update of such a pod changes neither its specification nor its labels.
The policy expects the Job controller to act as the ServiceAccount `kube-system:job-controller`, as controller managers do that run each controller with its own credentials, such as those of kubeadm and kind.

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

### Published Image Values

`tools/publish_images.py values` writes `chart/heartwood-platform/values-published.yaml`, which pins the [published images](../documentation/operate/platform-requirements.md#published-images) of one main-branch commit by digest: `images.platformServices`, `images.openshellGateway`, `images.hostedGateway`, `images.modelRuntime`, `images.funnel`, `hub.worker.image`, `sessions.stagingImages` and the worker's interpreter in `hub.modelRoute.binaries`.
Pass it after the cluster's values file, for example `--values values-gke.yaml --values values-published.yaml`.
The file is written and changed only in a reviewed change after a Native Qualification run on the main branch published the images, within the 90 days for which the run keeps its record:

```bash
commit=0123456789abcdef0123456789abcdef01234567  # the published commit
run="$(gh run list --repo SchmiedmayerLab/heartwood --workflow native-qualification.yml \
  --commit "${commit}" --status success --json databaseId --jq '.[0].databaseId')"
test -n "${run}"
gh run download "${run}" --repo SchmiedmayerLab/heartwood --name "published-images-${commit}" \
  --dir "${TMPDIR:-/tmp}/published-images"
python3 reference-platform/tools/publish_images.py values "${TMPDIR:-/tmp}/published-images/published-images.json"
```

`publish_images.py values` checks that the record names every image under the commit's tag by digest, runs `gh attestation verify` for the record and for each digest, bound to the Native Qualification workflow, the main branch and the commit, and only then writes the file.
The change names the commit and the run.

The workflow's `publish-images` job runs `publish_images.py build` and then `publish_images.py push`, and logs in to the registry between the two.
`build` requires a checkout of the commit without changes to tracked files and builds the images as the qualification does.
`push` refuses to replace an existing `sha-<commit>` tag, fails when the registry cannot be read, and writes the record `published-images.json` with the digest that each push reported.

## Funnel Image

The job executor is Funnel, built from the source and the patches that `images/funnel/funnel.json` records.
The manifest names the release tag, its revision and the archive of that revision.
GitHub generates revision archives on request and does not keep their bytes stable, so the manifest records `tree_sha256`, the SHA-256 digest of the extracted tree, computed over each path, its kind, its executable bit, the digest of its content and the target of each link.
`tools/prepare_funnel.py` verifies the source with the same extraction and digest as `deploy/build-worker-candidate.py`: the data filter, bounds on the archive and extracted size, no hard links, and a single root directory whose tree matches the digest.
It verifies each patch against its SHA-256 digest and applies the patches in order with `git apply`, which refuses a patch whose context does not match, isolated from the caller's git configuration.
The patched tree must then match `patched_tree_sha256`.
It writes the patched tree to `OUTPUT/funnel`, where `OUTPUT` is an empty directory and the build context of `images/funnel/Dockerfile`, and with `--image` builds the image.
With `--test` it first runs the tests of the packages that the patches change.
The Go modules are verified against the source's `go.sum`, and the build and base images are pinned by digest.

| Patch | Purpose |
|---|---|
| `0001-optional-tls-for-server-listeners-and-rpc-client.patch` | TLS on the HTTP and gRPC listeners of the server and in the gRPC client of the workers, verified against a configured CA file; without the settings Funnel behaves as the release |
| `0002-per-task-worker-credentials.patch` | A credential for each task, derived from a key file of the server and delivered to the task's worker in a Secret that the worker Job mounts; it may read only its own task, claim its execution and write its events |
| `0003-executor-template-gpu-count.patch` | Pass the validated GPU count to the executor Job template |

Each entry of the manifest also names the upstream issue, or the related issue where none exists, and the upstream change that would allow its removal.
When the pin changes, `prepare_funnel.py --print-tree-digest` prints the digest of the new archive, which is recorded only after it matches a reviewed checkout of the revision.

```bash
python3 reference-platform/tools/prepare_funnel.py "${TMPDIR:-/tmp}/funnel-context" --image heartwood-funnel:candidate --test
```

## Checks

The [Reference Platform workflow](../.github/workflows/reference-platform.yml) runs the cloud boundary check, the offline OpenTofu checks, the platform service checks, the chart template tests, the OpenShell render comparison, and the Funnel build with the tests of its patched packages.
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
docker build --file images/Dockerfile --target runtime-image --tag heartwood-runtime:candidate .
runtime="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-runtime:candidate)"
python3 reference-platform/tools/prepare_funnel.py "$(mktemp -d)/funnel" --image heartwood-funnel:candidate
funnel="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-funnel:candidate)"
bash reference-platform/cluster/install-chart.sh heartwood --set "images.platformServices=${image}" \
  --set "images.hostedGateway=${hosted}" --set "images.modelRuntime=${runtime}" \
  --set "images.funnel=${funnel}"
(cd reference-platform/services && uv run pytest -c pyproject.toml ../tests/cluster --cluster-context kind-heartwood \
  --ignore ../tests/cluster/test_hub_spawning.py --ignore ../tests/cluster/test_hosted_recovery.py \
  --ignore ../tests/cluster/test_hosted_worker.py --ignore ../tests/cluster/test_end_to_end.py)
bash reference-platform/cluster/delete-cluster.sh heartwood
```

The Hub's tests in `test_hub_spawning.py`, `test_hosted_recovery.py`, `test_hosted_worker.py` and `test_end_to_end.py` start Heartwood gateways, which require an OpenShell gateway with the recorded patches, so they run with the images of [Worker Qualification](#worker-qualification).
`test_hosted_worker.py` and `test_end_to_end.py` also need the worker image in `hub.worker.image` and its interpreter in `hub.modelRoute.binaries`.

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
The budget overview shows the chart's allocations and the reservation of each launched session, a launch and a deletion change it, and it answers only for the project of the token.
`test_tes_contract.py` holds the contract of the TES executor that the job service relies on, using only the executor's URL, CA and credential: a task that reads a presigned input and writes its output completes, a failing command is an executor error with its exit code, a missing input and an output that is not written are system errors, a cancelled task's pods are removed, cancelling a finished task leaves its state, an unknown task does not exist, tasks are listed by tag and state, and states only move forward.
`test_job_service.py` runs a synthetic batch job and a cancellation through the job service's tools with a researcher's exchanged token, and checks the refusal of another project, a GPU request and a reserved tag.
It also checks that a task created around the job service is cancelled, that an executor pod has no token, no credential and no network, that a worker credential reaches only its own task, that one project's credential is refused by another project's server, and that sessions do not reach the executor API.
Requests with a token bound to a worker pod show that the admission policy admits an executor Job of the worker's own task and refuses one of another task, one that mounts another task's claim or a Secret, and one with another ServiceAccount, a token or no user namespace.
`test_end_to_end.py` runs the platform as two synthetic researchers use it.
Both sign in to the Hub; the first starts the servers of projects demo and other, the second the server of project demo, and each gateway starts with its session's launch context.
In the first researcher's demo session, a worker on the OpenShell Kubernetes driver follows a script of the synthetic model through the model route: it lists, resolves and reads the project's dataset through the data catalog and the read-only data volume, where a write fails, runs a batch job and cancels a second job through the job service.
The researcher approves each call of the worker, the gateway's proposal of its outside operation and the release of its result to the worker.
The tests check the batch job's result in the project's bucket and that the proposal quotes the project's compute budget.
The second researcher is refused the first researcher's servers, the dataset of project other and the first researcher's jobs, and the tokens of a demo session reach neither the dataset nor the jobs of project other.
The session's audit export verifies with the application's verifier and names the researcher by the keyed actor, never by subject or user name, and deleting the server settles the session's inference reservation.

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
interpreter="$(jq --compact-output '[.worker_interpreter]' test-results/kubernetes-qualification/worker/images.json)"
hosted="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-hosted-gateway:qualification)"
runtime="$(bash reference-platform/cluster/push-image.sh heartwood heartwood-runtime:qualification)"
bash reference-platform/cluster/install-chart.sh heartwood --set "images.platformServices=${image}" \
  --set "images.modelRuntime=${runtime}" \
  --set "images.openshellGateway=${gateway}" --set "images.openshellRepository=${openshell}" \
  --set "images.hostedGateway=${hosted}" --set "images.funnel=${funnel}" --set "hub.worker.image=${worker}" \
  --set-json "sessions.stagingImages=${staging}" --set-json "hub.modelRoute.binaries=${interpreter}"
(cd reference-platform/services && uv run pytest -c pyproject.toml ../tests/cluster \
  --cluster-context kind-heartwood --staging-image "${worker}")
python3 deploy/qualify-kubernetes-worker.py run --cluster heartwood \
  --output test-results/kubernetes-qualification/worker
```

`images` verifies the gateway build, packages the gateway image, builds the controller image and pushes both with the worker image to the cluster's registry.
It also copies the supervisor and sandbox runtime images of the OpenShell release to the cluster's registry under their digests, so the cluster pulls every image of a worker from there, and builds and pushes the symbolic link workspace and replacement worker images of the Docker qualification.
`run` launches a session as the spawner would, starts the controller pod with that session's gateway ServiceAccount, runs the cases and removes the session.
[Kubernetes Driver Qualification](../documentation/architecture/testing.md#kubernetes-driver-qualification) lists the cases and the evidence.
