<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Audit Checkpoints and Retention

Heartwood keeps a content-minimized, hash-chained audit record with each session.
An ordinary audit export supports project review.
A signed checkpoint is the artifact a deployment can retain as authoritative evidence outside the agent-writable project.

## Choose the Right Artifact

| Artifact | Use | Storage |
|---|---|---|
| Session replay | Inspect private conversation and action history | Private `.heartwood/` session state |
| Scrubbed audit export | Review or transfer content-minimized operational events | Temporary reviewed destination |
| Signed audit checkpoint | Preserve an authenticated export with deployment and retention metadata | Deployment-controlled records storage outside the project |

A checkpoint contains only `audit.jsonl` and `checkpoint.json`.
Its signature covers the canonical audit digest, event count, terminal chain hash, session, deployment identifier, creation time, retention declaration, signer identity, key identifier, key version, algorithm, and public-key fingerprint.

## Understand Configuration Ownership

Heartwood separates project selection from deployment trust:

| Scope | Default location | Contents |
|---|---|---|
| Project | `.heartwood/config.toml` | An optional approved signer profile name only |
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

List the profiles visible to a project:

```bash
heartwood audit signer list
```

Use the deployment default, or select another approved profile for the current project:

```bash
heartwood audit signer default
heartwood audit signer select stanford-records
```

Only the selected profile name is written to `.heartwood/config.toml`.
A deployment that permits no user choice should publish only one profile.

## Use the Explicit Local Fallback

Development and offline environments can run the bundled loopback-only signer service.
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

The local service accepts authenticated loopback requests only.
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

## Rotate Keys and Enforce Retention

Publish a new registry profile or key version before directing new checkpoints to a rotated key.
Keep retired public keys and their profile mapping for as long as any corresponding checkpoint must remain verifiable.
Do not replace a trusted public key in place while retaining the same key version.

Signed retention fields are declarations, not a storage lifecycle engine.
The deployment must enforce access control, immutability where required, backup, replication, legal hold, expiration, deletion, and key rotation in its records system.
Map each trusted signer, public key, and `deployment-id` to deployment identity outside the checkpoint.

Before production use, complete a synthetic restore test that verifies a retained checkpoint and confirms that the records system applies the declared policy.
