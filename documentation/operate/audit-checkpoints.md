<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Audit Checkpoints and Retention

Every session keeps a content-minimized, hash-chained audit record.
A signed checkpoint authenticates one export of it, with a signed deployment and retention statement, for storage outside the agent-writable project.
Its authority comes from independently trusted keys, signer authorization and the records system's own controls.

There are two ways to create checkpoints:

- the explicit `heartwood audit checkpoint` command, which uses local project state and does not read the deployment manifest; and
- deployment-bound managed evidence, selected by the manifest's `[evidence]` table, which checkpoints automatically and gates dispatch.

Checkpoint creation is not a gateway HTTP route.

## Choose the Right Artifact

| Artifact | Use | Storage |
|---|---|---|
| Session replay | Inspect private conversation and action history | Session state in the project's state directory |
| Scrubbed audit export | Review or transfer content-minimized operational events | A reviewed temporary destination |
| Signed audit checkpoint | Preserve an authenticated export with deployment and retention metadata | Deployment-controlled records storage outside the project |

A checkpoint directory contains `audit.jsonl` and `checkpoint.json`, and optionally `experiments.jsonl`.
Its signature covers the canonical audit digest, event count, terminal chain hash, session, deployment identifier, creation time, retention declaration, signer identity, key identifier, key version, algorithm and public-key fingerprint.

### Evidence Boundaries

Verifying one checkpoint establishes its integrity and authenticity against the selected key.
It does not establish continuity between checkpoints, detect an omitted later checkpoint, or prove that the signer observed the recorded actions.
A valid hash chain alone cannot rule out a history rewritten and rehashed before signing, and worker-reported results remain claims unless the destination or the outside control plane verifies them.
Signed retention dates do not prove that the records system enforced immutability, availability or deletion.

### Automatic Evidence Boundaries

Managed evidence checkpoints every approval and rejection decision and the closure of a session owner.
Closing an owner releases its writer lease; it does not delete the session.
Each checkpoint binds the previous checkpoint's digest and extends its exact audit prefix, and the signer validates project, session, deployment, retention policy and retained history before publishing it.
The records adapter uses conditional writes, version-aware reads and COMPLIANCE retention observations, and rejects hidden versions, delete markers, missing ordinals and changed retained bytes.

In `required` mode, a decision stays pending until its checkpoint has been read back and verified from retained storage.
The evidence policy is retained with the decision, so removing the `[evidence]` table or switching to `buffered` cannot downgrade a pending decision.
In `buffered` mode, ordinary work continues during a records or signing outage with a visible evidence gap.
Approved outside operations and outside-result releases require retained evidence in both modes; a signature without observed retention never satisfies that gate.

An outside approval is recorded as authorized, checkpointed, then marked submitting before the native call.
It is submitted only by an exact retry of its own approval command, after current input, policy, expiry, destination and worker checks; reads and reconciliation never submit it.
Once submission may have occurred, Heartwood observes the native operation instead of repeating it.

Managed evidence does not checkpoint every model request, prove a worker's claims, or qualify the signing service or storage.
`AnchoredCheckpointSigner` in `heartwood.audit` composes the signer and records interfaces for an independent signing authority, but it is a library, not a deployed service: the operator supplies authenticated hosting, caller-to-scope authorization, key custody and storage policy.

### Verification at a Boundary

The gateway and the signing authority each keep the history they have verified and the observed version identifier and entity tag of every retained object it covers.
The first boundary after the gateway starts, and the first request the signing authority serves, verify the complete retained history: every checkpoint, its signature, its audit candidate and its link to its predecessor.
A later boundary, and a later signing request, extend that verification instead.
They list the session's records versions once and read only the checkpoints published since, with their candidates; the signed hash chain covers the earlier checkpoints.
The number of records read at a boundary does not depend on the session's length; the version listing still grows with it, one request for each thousand retained objects.
Before a new checkpoint is published, the gateway and the signing authority each reject a modified, missing, reordered or replaced checkpoint, candidate or experiment export they have already verified, and any delete marker in the session's records.
A failed boundary or request discards the kept history, so the next one verifies the complete history again.

Only a complete verification rereads retained bytes.
A store that changes an object's bytes without changing its version identifier or entity tag violates S3 versioning; the next complete verification detects such a change, after a gateway or authority restart or through the controller's explicit `verify` method.
The `heartwood audit verify` command and checkpoint creation still verify the complete local session and audit streams.

## Configure Managed Evidence

Add an `[evidence]` table to the [deployment manifest](../reference/configuration.md#deployment-manifest).
It requires a `[worker]` table and protected project identity, and Heartwood never reads records credentials, endpoints or signer selections from project content.

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

| Key | Meaning |
|---|---|
| `mode` | `required` or `buffered` |
| `deployment_id`, `region` | Identifiers of letters, digits, `_`, `.` and `-` |
| `endpoint` | HTTPS service origin with no credentials, path, query or fragment |
| `bucket` | Existing S3 bucket name |
| `prefix` | Slash-separated prefix of letters, digits, `_` and `-` |
| `retention` | `policy_id` and `retain_until` as `YYYY-MM-DD`; an expired date prevents startup |
| `signer_registry`, `signer_profile` | Registry path and a production-mode profile in it |
| `previous_signer_profiles` | Unique profile names accepted only to verify retained history after key rotation |
| `credential_process` | Executable returning version-1 credential-process JSON |
| `credentials_file`, `credentials_profile` | Owner-only AWS shared-credentials file and the profile in it |
| `ca_bundle` | Optional operator-controlled CA file for the records service |

Select exactly one credential source: `credential_process`, or `credentials_file` with `credentials_profile`.
All paths are absolute and must be operator-owned, write-protected and outside research files and state.
The credential process runs without arguments from its own directory, with a fifteen-second deadline and a 64 KiB output limit, and its error output is not shown; include `Expiration` for temporary credentials. See the [SDK credential-process contract](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sourcing-external.html).
A shared credentials file is read once per gateway lifetime, so use the credential process when credentials must refresh during a session.
Ambient proxy, profile, endpoint, certificate, environment and metadata credentials are never used as fallbacks, and no credential reaches the worker.

When the manifest policy lists `allowed_checkpoint_signer_profiles`, `signer_profile` must be one of them, and the same list bounds the profile a project may select for standalone checkpoints.
Set `checkpoint_evidence = true` under `[requirements]` to make a manifest without required-mode evidence fail at launch.
Restart the gateway after changing the signer profile, token or trust set, and confirm continuity before resuming work.
Do not edit a retained scope's retention declaration; exact scope validation rejects that change.

### Records Permissions and Retention

Use an existing versioned bucket with Object Lock and an independently administered retention policy; Heartwood neither creates the bucket nor grants storage privileges.
Each session has a deterministic namespace under `prefix`, with `candidates`, `experiments` and `checkpoints` children.

| Principal | Read access | Write access |
|---|---|---|
| Gateway | Version listing, exact object versions and retention metadata within its scope | Conditional candidate and experiment creation with the declared retention |
| Signing authority | Authorized candidates and complete retained checkpoint history with retention metadata | Conditional checkpoint creation with the declared retention |
| Worker | None | None |

On Amazon S3, the adapter uses `s3:ListBucketVersions`, `s3:GetObjectVersion` and `s3:GetObjectRetention`, plus `s3:PutObject` and `s3:PutObjectRetention` on each principal's writable prefixes.
Scope list permissions with prefix conditions and object permissions with exact resource patterns; encryption may need separately scoped key permissions. See [S3 operation permissions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-with-s3-policy-actions.html) and [Object Lock visibility](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html).
Neither identity needs bucket administration, object deletion, governance bypass, ACL changes or access to another prefix.
Enforce `If-None-Match` writes in the bucket policy as well, because versioning and retention alone do not prevent a new version from hiding an older one. See [S3 conditional-write enforcement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html).
Qualify equivalent behavior for any other S3-compatible store.

Native qualification runs the records-service and transport suites against a synthetic MinIO server built from the revision pinned in `images/worker/candidate/records.json`, with versioning, Object Lock in COMPLIANCE mode and the conditional-write bucket policy above.
Each suite records the store, the policy and `conditional_write_enforcement` in its `records-result.json`.
That server rejects the `s3:if-none-match` condition key, so its results record `conditional_write_enforcement: false` and `records_policy_qualified: false`.
It refuses a conditional overwrite of a published checkpoint but accepts an unconditional write as a new version.
Heartwood rejects such a version wherever it reads the affected history, yet create-only publication is not enforced by that store.
The records-service suite checks retention denial, lost-reply reconciliation, concurrent publication, outage recovery, backup and restore into a fresh server, and delete-marker and replacement-version detection against that same configuration.
Create-only publication still needs qualification against a store that enforces the conditional-write policy, together with institutional retention approval and a production KMS or HSM signer.

A working manifest does not prove that the signer authorizes requests, that the storage identities are restricted, or that the institution approves the deployment.
Verify authorized and denied operations, key rotation, interrupted publication, retention, backup and restore, and network boundaries with synthetic records before admitting sensitive data.

## Infrastructure Telemetry

OpenShell can export its native OCSF events through OTLP to an authenticated collector.
Configure the sandbox gateway's `openshell.gateway.otlp.ocsf` section with `enabled`, the collector `endpoint`, and operator-controlled `ca_file`, `cert_file` and `key_file` paths, and require a collector that validates client certificates.
Correlate `openshell.gateway.name` and `openshell.sandbox.id` with the worker attempt, and keep native event identifiers for deduplication.

Delivery is best effort and can repeat records.
An `openshell.ocsf.gap` event reports dropped records through `openshell.ocsf.gap.dropped` and `openshell.ocsf.gap.since_unix_ms`, but its absence does not prove complete collection.
Heartwood does not gate dispatch on collector health; stopping work when observation is unavailable needs outside enforcement.
Infrastructure events do not replace the canonical decision record.
See [Infrastructure Telemetry qualification](../architecture/testing.md#infrastructure-telemetry).

## Understand Configuration Ownership

| Scope | Default location | Contents |
|---|---|---|
| Project | `config.toml` in the state directory | An approved signer profile name only |
| Managed deployment | `/etc/heartwood/checkpoint-signers.toml` | Approved signer endpoints, trusted public keys and the deployment default |
| Workstation fallback | `~/.config/heartwood/checkpoint-signers.toml` | Local development or offline signer profiles |
| Signer credential | Separate owner-only file | Optional bearer token for signer requests |
| Signing key | Remote KMS, HSM or isolated local signer process | Private key material, never project state |

`HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY` names one absolute registry path where `/etc` is unavailable.
Heartwood loads the explicit path first, then the system registry, then the user fallback, and never merges registries.
The Carina launcher preserves an explicit registry path inside a Slurm allocation without exporting the signer token or provider credentials.
A project cannot define an endpoint, key, credential or private-key path, so copying a project cannot redirect signing.

## Configure a Production Signer

Expose the signer contract through an authenticated HTTPS service backed by a key management service (KMS) or hardware security module (HSM).
It accepts `heartwood.audit-checkpoint-sign-request.v1` at `/v1/checkpoints/sign` and returns `heartwood.audit-checkpoint-signature.v1`, using Ed25519 or ECDSA P-256 with SHA-256 (DER-encoded).
The service must authorize the deployment through a principal unavailable to agent tools, validate deployment and retention identifiers, restrict key use, rate-limit requests and keep provider audit records; it must not be an unrestricted signing oracle.

```toml
schema_version = "heartwood.checkpoint-signer-registry.v1"
default_profile = "research-records"

[profiles.research-records]
mode = "production"
endpoint = "https://checkpoint-signer.example.org/v1/checkpoints/sign"
signer_id = "research-records"
key_id = "heartwood-audit"
key_version = "2026-08"
algorithm = "ecdsa-p256-sha256"
public_key_sha256 = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
trusted_public_key = "/etc/heartwood/keys/heartwood-audit-2026-08.pem"
authorization_token_file = "/run/secrets/heartwood-checkpoint-signer-token"
timeout_seconds = 15
```

`authorization_token_file` is optional when an authenticated proxy protects the route.
An owner-only token file is readable by every process running as that owner, so use one only when agent tools cannot run as that identity; otherwise enforce authorization in a separate proxy and omit the token.
All paths are absolute; the registry and public key must not be writable by others and the token must be an owner-only regular file.
Heartwood rejects symbolic links, oversized files, changed key fingerprints, unapproved algorithms, insecure production endpoints and unknown fields.
Signing uses the bundled public certificate authorities, ignores environment proxy and certificate settings and rejects redirects; `trusted_ca_bundle` adds a private CA file without replacing the pinned verification key.

List the profiles visible to a project, return to the default, or select another approved profile:

```bash
heartwood audit signer list
heartwood audit signer default
heartwood audit signer select research-records
```

Only the profile name is written to `config.toml` in the state directory.
A deployment that permits no choice publishes one profile.

## Use the Explicit Local Fallback

Development and offline environments can run the bundled signer on a private Unix socket:

```bash
heartwood signer init-local
heartwood signer serve-local
heartwood audit signer select local-development
```

`init-local` creates a user registry, trusted public key, owner-only token and owner-only Ed25519 key under `~/.config/heartwood/`, refuses to replace existing material and prints the next commands.
Run `serve-local` in a separate terminal and select the profile in the project that creates checkpoints.
The client validates the socket's owner, permissions and owner-only directory before sending the token, and there is no TCP fallback.
After an abnormal shutdown, confirm the signer has stopped before removing its stale socket.
These files are readable by unsandboxed processes running as the same user, so local verification is not independent of the account.

## Verify and Checkpoint a Session

Run these commands from the project that owns the session:

```bash
heartwood --session-id session-main audit verify

heartwood --session-id session-main audit checkpoint \
  --output /records/heartwood/session-main-2026-08-02 \
  --deployment-id research-environment \
  --retention-policy research-audit-7y \
  --retain-until 2033-08-02
```

Creation records an audit-export event, verifies the session and audit streams, canonicalizes the export, asks the active signer to sign, verifies the returned identity and signature, and publishes the directory atomically.
The output must be outside the project and must not exist.

Verify a copied or restored checkpoint against the active profile, or against a separately retained public key:

```bash
heartwood audit verify-checkpoint /records/heartwood/session-main-2026-08-02
heartwood audit verify-checkpoint /records/heartwood/session-main-2026-08-02 \
  --public-key /records/trust/heartwood-audit-2026-08.pem
```

Verification rejects altered audit content, a broken chain, changed signed metadata, an invalid signature, an unexpected file, noncanonical encoding or a mismatched key.

## Retain Experiment Records

Add `--include-experiments` to retain the project's [experiment records](../architecture/experiments.md) in the same checkpoint:

```bash
heartwood --session-id session-main audit checkpoint \
  --include-experiments \
  --output /records/heartwood/session-main-with-experiments \
  --deployment-id research-environment \
  --retention-policy research-audit-7y \
  --retain-until 2033-08-02
```

The gateway verifies and snapshots the records of every session in the project, records only the snapshot's digest, size, filename and binding format in the audit, and retains the export as `experiments.jsonl`.
An empty project produces a bound empty export, not a claim that analyses completed.
`heartwood audit verify-checkpoint` also rejects a missing or altered experiment export, an unbound file, or a binding that does not belong to the terminal audit-export event.

If signing fails, restore the signer and retry with a new output path; this adds another audit-export event but does not rerun an analysis.
If a directory exists after an interrupted publication, verify it before choosing a new destination.
Experiment metadata can include project-relative paths, declared actor references and environment fingerprints, so include it only where the destination's access rules permit.
The signature does not establish scientific correctness or authenticate a declared researcher.

## Rotate Keys and Enforce Retention

Publish a new profile or key version before directing checkpoints to a rotated key, and keep retired public keys mapped to their profile for as long as their checkpoints must verify.
Never replace a public key in place under the same key version.
The records system, not Heartwood, enforces access control, immutability, backup, replication, legal hold, expiration and deletion.
Before production use, restore a retained checkpoint in a synthetic test, verify it, and confirm that the records system applies the declared policy.
