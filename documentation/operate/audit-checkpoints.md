<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Audit Checkpoints and Retention

Heartwood keeps a content-minimized, hash-chained audit record with each session.
An ordinary audit export supports local review, while a signed checkpoint creates the artifact that a deployment can retain as authoritative evidence outside the agent-writable project.

## Choose the Right Artifact

| Artifact | Use | Storage |
|---|---|---|
| Session replay | Inspect private conversation and action history | Private `.heartwood/` session state |
| Scrubbed audit export | Review or transfer content-minimized operational events | Temporary reviewed destination |
| Signed audit checkpoint | Preserve an authenticated export with deployment and retention metadata | Deployment-controlled records storage outside the project |

The signed checkpoint contains only `audit.jsonl` and `checkpoint.json`.
The checkpoint binds the canonical audit digest, event count, terminal chain hash, session, deployment identifier, creation time, and retention declaration to an Ed25519 signature.

## Prepare a Deployment Key

Create and retain the signing key outside every Heartwood project.
The private key must be an owner-only regular file; Heartwood rejects group-readable, world-readable, linked, oversized, encrypted, or non-Ed25519 private keys.

```bash
umask 077
openssl genpkey -algorithm ED25519 -out /secure/heartwood/audit-private.pem
openssl pkey \
  -in /secure/heartwood/audit-private.pem \
  -pubout \
  -out /secure/heartwood/audit-public.pem
chmod 600 /secure/heartwood/audit-private.pem
```

Distribute the public key through a separately trusted configuration or records process.
The key identifier in a checkpoint is a SHA-256 digest of that public key; it does not establish trust by itself.

## Verify and Checkpoint a Session

Run these commands from the Heartwood project that owns the session:

```bash
heartwood --session-id session-main audit verify

heartwood --session-id session-main audit checkpoint \
  --output /records/heartwood/session-main-2026-08-02 \
  --deployment-id research-environment \
  --retention-policy research-audit-7y \
  --retain-until 2033-08-02 \
  --signing-key /secure/heartwood/audit-private.pem
```

Checkpoint creation first records a new audit-export event, fully verifies the paired session and audit streams, canonicalizes the export, signs its statement, and publishes the complete directory atomically.
The output and signing key must resolve outside the Heartwood project.
Heartwood refuses to replace an existing checkpoint directory.

Verify a copied or restored checkpoint against the independently trusted public key:

```bash
heartwood audit verify-checkpoint \
  /records/heartwood/session-main-2026-08-02 \
  --public-key /secure/heartwood/audit-public.pem
```

Verification rejects an altered audit record, broken hash chain, changed statement, invalid signature, unexpected file, noncanonical encoding, or mismatched key.

## Enforce Records Policy

The signed retention fields are a declaration, not a storage lifecycle engine.
The deployment must enforce access control, immutability where required, backup, replication, legal hold, expiration, deletion, and key rotation in its records system.

Map each trusted public key and `deployment-id` to deployment identity outside the checkpoint.
Keep retired public keys for as long as any checkpoint signed by them must remain verifiable.
Never place private signing keys, authoritative checkpoints, or records-system credentials inside the agent project or container image.

Before production use, perform a synthetic restore test that verifies the checkpoint with the retained public key and confirms that the records system applies the declared policy.
