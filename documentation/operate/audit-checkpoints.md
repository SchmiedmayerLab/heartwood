<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Audit Checkpoints and Retention

Heartwood keeps a content-minimized, hash-chained audit record with each session.
An ordinary audit export supports project review.
A signed checkpoint authenticates an export and its signed deployment statement for retention outside the agent-writable project.
Its authority depends on independently trusted signing keys, signer authorization and the records system's controls.

The command examples below use local project state.
The standalone audit CLI does not accept the gateway's `--deployment` manifest; it must not be used as though it automatically selects protected external state.
A managed operator integration must use the explicitly bound gateway's checkpoint methods and preserve the same identity, policy and writer-ownership checks.
Checkpoint creation is not exposed as a gateway HTTP route.

## Choose the Right Artifact

| Artifact | Use | Storage |
|---|---|---|
| Session replay | Inspect private conversation and action history | Private session state in the bound state directory |
| Scrubbed audit export | Review or transfer content-minimized operational events | Temporary reviewed destination |
| Signed audit checkpoint | Preserve an authenticated export with deployment and retention metadata | Deployment-controlled records storage outside the project |

A standard checkpoint contains `audit.jsonl` and `checkpoint.json`.
Its signature covers the canonical audit digest, event count, terminal chain hash, session, deployment identifier, creation time, retention declaration, signer identity, key identifier, key version, algorithm, and public-key fingerprint.
An operator can also include a verified project experiment export, as described below.

### Evidence Boundaries

Verification of an individual checkpoint establishes its integrity and authenticity against the selected trusted key.
It does not establish continuity between separate checkpoints, detect an omitted later checkpoint or prove that the signer independently observed the recorded actions.
The automatic evidence controller additionally verifies independently retained history as described below; standalone exports do not acquire that guarantee.
A valid local hash chain alone cannot rule out a rewritten history whose hashes were recomputed before signing.
Worker-reported results remain claims unless the destination or outside control plane independently verifies them.

The standalone checkpoint command creates an explicit operator export; selecting a signer profile alone does not enable automatic checkpointing or gate dispatch.
Trusted gateway integrations can bind an external evidence controller to protected session state, an independently authorized signer and an existing S3-compatible records system.
The gateway deployment manifest selects this controller through its `evidence` table and requires a complete worker-isolation configuration.
Signed retention dates alone do not prove that the records system enforced immutability, availability or deletion policy.

### Automatic Evidence Boundaries

A bound evidence controller checkpoints approval and rejection decisions, and the closure of a session owner.
Closing an owner releases its writer lease; it does not delete the session or prevent a later authorized owner from reopening it.
In required mode, a decision remains pending until the controller reads and verifies its independently retained checkpoint.
The required evidence policy is retained atomically with its decision; removing the controller or selecting buffered mode cannot downgrade that pending decision.
Recovery uses the same publication identity and prepared snapshot; it does not replace a retained ordinal or silently create a new decision.
In explicitly selected buffered mode, ordinary local work may continue during a records or signing outage, with a visible evidence gap.
Approved outside operations and disclosure of outside results require retained evidence even in buffered mode.
A plain signature without observed retention never satisfies this gate.

The signer validates the project, session, deployment, retention policy and complete retained history before publication.
Each checkpoint binds the previous complete checkpoint digest and extends its exact canonical audit prefix.
The records adapter uses conditional publication, version-aware reads and COMPLIANCE retention observations; hidden versions, delete markers, missing ordinals and changed retained bytes are rejected.
The records system must independently enforce storage authorization and retention.
Gateway credentials may stage candidates and read retained evidence; only the independently authorized signer may publish checkpoint ordinals.
Neither credential nor trusted signing roots belong in worker access.

An outside approval is first recorded as authorized, then checkpointed, then marked submitting before the native call.
A retained authorization can resume only after current input, policy, expiry, destination and worker authority checks.
Read-only reconciliation never submits it, and recovery does not revive a worker after restart.
Once submission may have occurred, Heartwood observes the native operation identity instead of repeating the effect.
These controls do not automatically checkpoint every model request, prove a worker's claims or qualify an institution's storage, signing service or sensitive-data deployment.

## Configure Managed Evidence

Add an `evidence` table to the same operator-owned deployment manifest as the [worker configuration](../reference/cli.md#operator-commands).
The launcher rejects evidence configuration without a complete worker selection, protected project identity or valid external authority files.
It does not discover records credentials, endpoints or signer selections from project content.

```toml
[evidence]
mode = "required"
deployment_id = "research-production"
endpoint = "https://records.example.org"
region = "us-east-1"
bucket = "research-audit-records"
prefix = "heartwood/project-a"
signer_registry = "/etc/heartwood/checkpoint-signers.toml"
signer_profile = "research-records"
previous_signer_profiles = []
credential_process = "/usr/local/libexec/heartwood-records-credentials"

[evidence.retention]
policy_id = "research-records-retention"
retain_until = "2033-12-31"
```

Use the exact HTTPS service origin, with no credentials, query or path in its URL.
TLS uses the bundled public certificate authorities unless `ca_bundle` names an operator-controlled CA file.
Ambient proxy, profile, endpoint and certificate settings do not select the records route or identity.
The storage SDK handles request signing and temporary-credential refresh.
The selected credential helper is an absolute executable outside research files and state, invoked without arguments from its own directory.
It returns the SDK's version-1 credential-process JSON; include `Expiration` for temporary credentials and let the existing institutional identity service issue them. See the [SDK credential-process contract](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sourcing-external.html).
Each helper invocation has a fifteen-second deadline and a 64 KiB result limit; helper error output is not included in public errors.

For an explicitly provisioned shared credential file, replace `credential_process` with `credentials_file = "/run/secrets/records-credentials"` and `credentials_profile = "heartwood-records"`.
The file uses the standard AWS shared-credentials format and must be an owner-only regular file outside research files and state.
A missing selected profile fails closed; environment credentials, metadata credentials and other profiles are not fallback identities.
Shared-file credentials are loaded for the gateway lifetime; replacing that file requires restarting the gateway.
Use the credential-process route when temporary credentials must refresh during a session.
Neither route copies credentials into the worker, its environment, command arguments or the research directory.

The selected signer profile must use production mode in the existing registry.
Previous signer profile names authorize verification of retained history after key rotation; they do not select another signing service for new requests.
Keep those verification keys available while their retained checkpoints are needed.
Restart after changing the signer profile, token or trust set, and qualify continuity before resuming work.
The deployment, project, session and retention declaration identify the records scope.
Do not edit a retained scope's retention declaration as a shortcut for migration; exact scope validation rejects that change.
Expired retention prevents startup.

### Records Permissions and Retention

Use an existing general-purpose versioned bucket with Object Lock and an independently administered retention policy.
Heartwood neither creates the bucket nor grants storage privileges.
Select a dedicated deployment/project prefix; each session has a deterministic namespace beneath it, with `candidates`, `experiments` and `checkpoints` children.
The gateway stages content-addressed candidates; the independent signer owns checkpoint publication.

| Principal | Read access | Write access |
|---|---|---|
| Gateway | Version listing, exact object versions and retention metadata within its records scope | Conditional candidate and experiment creation with the declared retention |
| Signing authority | The authorized candidates and complete retained checkpoint history, including retention metadata | Conditional checkpoint creation with the declared retention |
| Worker | None | None |

On Amazon S3, the adapter uses `s3:ListBucketVersions`, `s3:GetObjectVersion` and `s3:GetObjectRetention`, plus `s3:PutObject` and `s3:PutObjectRetention` on each principal's writable prefixes.
Scope bucket-list permission with prefix conditions and object permissions with exact resource patterns; encryption policies may require separately scoped key permissions. See [S3 operation permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html) and [Object Lock visibility](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html).
Neither application identity needs bucket administration, object deletion, governance bypass, ACL changes or access to another deployment's prefix.
Enforce `If-None-Match` writes in the bucket policy as well as the client; versioning and retention alone do not prevent a new version from hiding an older one. See [S3 conditional-write enforcement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html).
The adapter requests COMPLIANCE retention through the complete declared UTC date and verifies the observed mode and deadline on versioned reads.
Qualify equivalent permissions and behavior for another compatible store instead of assuming AWS policy syntax applies there.

A working manifest does not prove the outside signer authorizes requests, that storage identities have these restrictions, or that the selected institution approves the deployment.
Verify authorized operations and denied alternatives, key rotation, interrupted publication, retention, backup/restore and network boundaries using synthetic records before admitting sensitive data.

## Infrastructure Telemetry

Keep Heartwood's canonical decisions and signed checkpoints distinct from outside observations of sandbox activity.
The reference infrastructure supports OpenShell's native OCSF export through OTLP to an existing authenticated collector.
Configure the gateway's `openshell.gateway.otlp.ocsf` section with `enabled`, the exact collector `endpoint`, and operator-controlled `ca_file`, `cert_file` and `key_file` paths.
The infrastructure supplies export configuration to its supervisors; collector credentials and management access stay outside workers.
Require a collector that validates the client certificate, and restrict its network route and retained data to authorized operators.

Correlate `openshell.gateway.name` and `openshell.sandbox.id` with the outside worker-attempt binding, and preserve native event identifiers for deduplication.
Infrastructure events can contain sensitive operational metadata; apply the deployment's access, retention and export policy to them.
They do not independently prove the scientific correctness of a worker's results or replace the canonical decision record.

Delivery is best effort and can repeat records after a lost response.
The native `openshell.ocsf.gap` event reports dropped records through `openshell.ocsf.gap.dropped` and `openshell.ocsf.gap.since_unix_ms` on a subsequent exported batch.
Absence of a gap event does not prove complete collection, especially during inactivity, collector rejection or abrupt teardown.
Monitor collector availability, rejected exports and retention independently, and retain explicit unknown or missing intervals.
Heartwood does not gate dispatch on OTLP collector health; any requirement to stop work when infrastructure observation is unavailable needs outside deployment enforcement.
See [Infrastructure Telemetry qualification](../architecture/testing.md#infrastructure-telemetry) for the synthetic loss and ledger-preservation checks.

## Understand Configuration Ownership

Heartwood separates project selection from deployment trust:

| Scope | Default location | Contents |
|---|---|---|
| Project | `config.toml` in the bound state directory | An optional approved signer profile name only |
| Managed deployment | `/etc/heartwood/checkpoint-signers.toml` | Approved signer endpoints, trusted public keys, and the deployment default |
| Workstation fallback | `~/.config/heartwood/checkpoint-signers.toml` | Explicit local development or offline signer profiles |
| Signer credential | Separate owner-only file | Optional bearer token used only for the signer request |
| Signing key | Remote KMS, HSM, or isolated local signer process | Private key material; never project state |

A managed launcher can set `HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY` to one absolute deployment-controlled path when `/etc` is not available, including a managed container or shared-computing installation.
Heartwood loads the explicit path first, then the system registry, then the user fallback.
It loads one registry and never merges authorities from different scopes.
The Carina launcher preserves an explicit registry path when it enters a Slurm allocation.
It does not export the signer token or model-provider credentials; the allocated process reads the token from the deployment-owned file named by the registry.

The project cannot define an endpoint, trusted key, credential, or private-key path.
Copying a project therefore cannot redirect checkpoint creation to a project-controlled signer.

## Configure a Production Signer

Production deployments should expose the Heartwood signer contract through a small authenticated HTTPS service backed by a key management service (KMS) or hardware security module (HSM).
The service accepts `heartwood.audit-checkpoint-sign-request.v1` at `/v1/checkpoints/sign` and returns `heartwood.audit-checkpoint-signature.v1`.
Heartwood supports Ed25519 and ECDSA P-256 with SHA-256; P-256 signatures use the standard DER encoding.

The signing service must authenticate and authorize the deployment through a principal unavailable to agent tools, independently validate the requested deployment and retention identifiers, restrict key use, rate-limit requests, and retain provider audit records.
It must not operate as an unrestricted signing oracle.
Cloud-specific credentials and private keys remain in the service or its KMS/HSM integration, not in Heartwood.

Install a registry such as:

```toml
schema_version = "heartwood.checkpoint-signer-registry.v1"
default_profile = "stanford-records"

[profiles.stanford-records]
mode = "production"
endpoint = "https://checkpoint-signer.example.org/v1/checkpoints/sign"
signer_id = "stanford-research-records"
key_id = "heartwood-audit"
key_version = "2026-08"
algorithm = "ecdsa-p256-sha256"
public_key_sha256 = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
trusted_public_key = "/etc/heartwood/keys/heartwood-audit-2026-08.pem"
authorization_token_file = "/run/secrets/heartwood-checkpoint-signer-token"
timeout_seconds = 15
```

The token setting is optional when an authenticated deployment proxy protects the complete route without a Heartwood-supplied bearer token.
Use a token file in production only when the platform prevents agent tools from reading that file or invoking the signer as the same identity.
Owner-only permissions do not isolate a credential from another process running as that owner.
Where that isolation is unavailable, enforce operator or workload authorization in a separate proxy or control-plane service and omit the Heartwood-readable token.
The registry and public key may be readable but must not be writable by other users.
The token must be an owner-only regular file.
All paths must be absolute, and Heartwood rejects symbolic links, oversized files, changed key fingerprints, unapproved algorithms, insecure production endpoints, and unknown fields.

HTTPS signing uses the bundled public certificate authorities and ignores environment proxy and certificate settings.
For a private certificate authority, set `trusted_ca_bundle` in the signer profile to an absolute operator-controlled PEM certificate file outside research files and canonical state.
This adds TLS trust for the service; it does not replace the pinned checkpoint verification key or authorize the deployment to sign.
Redirects are rejected, and the CA setting is not accepted for a local Unix-socket signer.

List the profiles visible to a project:

```bash
heartwood audit signer list
```

Use the deployment default, or select another approved profile for the current project:

```bash
heartwood audit signer default
heartwood audit signer select stanford-records
```

Only the selected profile name is written to `config.toml` in the bound state directory.
That directory is local `.heartwood/` or the operator-bound external state root; signer keys and service credentials belong in neither.
A deployment that permits no user choice should publish only one profile.

## Use the Explicit Local Fallback

Development and offline environments can run the bundled signer on a private Unix socket.
Initialize it from any Heartwood project; its material is written outside that project by default:

```bash
heartwood signer init-local
```

The command creates a user registry, trusted public key, owner-only authorization token, and owner-only Ed25519 private key under `~/.config/heartwood/`.
It refuses to replace existing material and prints the exact next commands.

Keep the signer running in a separate terminal:

```bash
heartwood signer serve-local
```

Then select it in the project that will create the checkpoint:

```bash
heartwood audit signer select local-development
```

The local service accepts bearer-authenticated requests through the registry's `unix:///` endpoint only.
The socket must belong to the current operating-system user, have owner-only permissions, and reside in an owner-only directory with trusted, non-symlink ancestors.
The client validates this protected path before connecting and rechecks the socket identity before sending any token or request bytes.
The server refuses to replace an existing socket path; after an abnormal shutdown, an operator must confirm the signer has stopped before removing its stale socket.
There is no TCP fallback.
Keep the socket, key, token and registry outside the research directory and every worker mount.
These files remain accessible to unsandboxed processes running as the same user, so verification through this registry is not independent of the account.
It is an explicit development and offline fallback, not the production default and not a substitute for KMS/HSM access controls or independent authorization.

## Verify and Checkpoint a Session

Run these commands from the Heartwood project that owns the session:

```bash
heartwood --session-id session-main audit verify

heartwood --session-id session-main audit checkpoint \
  --output /records/heartwood/session-main-2026-08-02 \
  --deployment-id research-environment \
  --retention-policy research-audit-7y \
  --retain-until 2033-08-02
```

Checkpoint creation records a new audit-export event, fully verifies the paired session and audit streams, canonicalizes the export, asks the active signer to sign the statement, verifies the returned identity and signature against the registry, and publishes the complete directory atomically.
The output must resolve outside the Heartwood project.
Heartwood refuses to replace an existing checkpoint directory.

Verify a copied or restored checkpoint against the active deployment profile:

```bash
heartwood audit verify-checkpoint \
  /records/heartwood/session-main-2026-08-02
```

For an independent verification workflow, provide a separately retained public key:

```bash
heartwood audit verify-checkpoint \
  /records/heartwood/session-main-2026-08-02 \
  --public-key /records/trust/heartwood-audit-2026-08.pem
```

Verification rejects altered audit content, a broken hash chain, changed signed metadata, an invalid signature, an unexpected file, noncanonical encoding, or a mismatched key.

## Retain Experiment Records

Add `--include-experiments` when the retained checkpoint should also identify the project's recorded analyses:

```bash
heartwood --session-id session-main audit checkpoint \
  --include-experiments \
  --output /records/heartwood/session-main-with-experiments \
  --deployment-id research-environment \
  --retention-policy research-audit-7y \
  --retain-until 2033-08-02
```

The gateway verifies and snapshots the [project's experiment records](../architecture/experiments.md), including recorded scripts and workflow stages from all sessions in that project.
It records only the snapshot's digest, byte count, filename, and binding format in the session audit.
The same signed checkpoint then retains the exact scientific export as `experiments.jsonl`; no additional signer or private key is needed.
Later project changes do not change this retained snapshot.
An empty project collection produces a bound empty export, not a claim that analyses have been completed.

Use the same `heartwood audit verify-checkpoint` command to verify both files and their binding.
Verification rejects a missing or altered experiment export, an unbound added file, and a binding that does not belong to the terminal audit-export event.
Preserve the complete checkpoint directory when copying or restoring it.
If signing fails, restore signer availability and retry with an unused output path; this creates another audit-export event but does not rerun an analysis.
If a checkpoint directory already exists after an interrupted publication, verify it before choosing a new destination; Heartwood will not overwrite it.

Experiment metadata can include project-relative paths, declared actor references, and environment fingerprints even though raw file contents, arguments, and parameter values are excluded.
Include it only when the destination's access and retention rules permit that metadata.
The signature authenticates the retained bytes and deployment statement; it does not establish scientific correctness, independently authenticate a declared researcher, or prove that every dependency was declared.
Ordinary browser, notebook, and command-line experiment exports remain unsigned.
The explicit operator checkpoint command and the deployment-bound evidence controller can retain authenticated experiment snapshots outside the agent-writable project.

## Rotate Keys and Enforce Retention

Publish a new registry profile or key version before directing new checkpoints to a rotated key.
Keep retired public keys and their profile mapping for as long as any corresponding checkpoint must remain verifiable.
Do not replace a trusted public key in place while retaining the same key version.

Signed retention fields are declarations, not a storage lifecycle engine.
The deployment must enforce access control, immutability where required, backup, replication, legal hold, expiration, deletion, and key rotation in its records system.
Map each trusted signer, public key, and `deployment-id` to deployment identity outside the checkpoint.

Before production use, complete a synthetic restore test that verifies a retained checkpoint and confirms that the records system applies the declared policy.
