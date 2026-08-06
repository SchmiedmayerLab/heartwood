<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Configuration and State Reference

Heartwood derives the project from the process current directory.
Normal users do not set a Heartwood home, workspace, state root, model root, or session directory through environment variables.

## Project Layout

| Path | Contents |
|---|---|
| `.heartwood/config.toml` | Non-secret platform, model, action, policy, audit-signer profile, and Heartwood-managed model selection |
| `.heartwood/state.json` | State-schema marker |
| `.heartwood/sessions/` | Session metadata, events, audit chains, exports, and OpenHands persistence |
| `.heartwood/models/` | Downloaded or verified bundle-imported model artifacts and provenance |
| `.heartwood/skills/` | Explicitly installed project Skills |
| `.heartwood/audit/` | Project-level audit artifacts |
| `.heartwood/runtime/` | Runtime process and readiness state |
| `.heartwood/logs/` | Heartwood-managed inference and gateway diagnostics |
| `.heartwood/cache/` | Project-scoped model and runtime caches |

Heartwood creates the state root and children with private filesystem permissions and rejects symbolic-link substitutions.
The internal `.gitignore` excludes every state file from the surrounding Git repository.
The state marker records the current versions of each independently persisted Heartwood envelope.
Heartwood applies only registered deterministic forward migrations, under a process-shared native lock, and atomically replaces migrated metadata.
Unknown versions and malformed records fail closed rather than being rewritten heuristically.

## Configuration Ownership

Use the CLI or browser settings rather than editing `config.toml` manually.
Writes are validated, atomic, and protected by a project-scoped configuration lock.

The file may contain endpoint URLs, model identifiers, credential binding names, policy settings, and artifact provenance.
It must never contain raw credential values.

### Audit Signer Selection

The optional project audit setting contains only a profile approved by the active deployment registry:

```toml
[audit]
signer_profile = "stanford-records"
```

Use `heartwood audit signer list`, `select`, and `default` instead of editing this value directly.
Signer endpoints, trusted keys, authorization tokens, and private keys are never project configuration.

## Credential Binding Names

Built-in provider profiles use environment-style names such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, and `STANFORD_AI_API_KEY`.
The name identifies a secret source; it is not the secret.

On a supported workstation, an explicitly remembered value is stored by the operating-system keyring under a project-scoped account.
Containers and managed platforms normally use process entry, a mounted secret file, or managed identity.

## ChatGPT Account State

The **Sign in with ChatGPT** connection delegates OAuth, token storage, refresh, supported-model discovery, and request transport to OpenHands.
OpenHands stores that account credential in its private user-level credential store, outside `.heartwood/`, so the account can be reused across projects for the same operating-system user.
Heartwood project state retains non-secret connection and model-profile metadata, including the selected model; it does not retain the account credential.
Disposable containers require an explicit private credential-directory mount to retain this state after the container exits.

Run `heartwood models forget openai-subscription` or use **Sign out** in browser settings to remove the OpenHands credential.

## Deployment Environment

Environment variables remain valid at platform boundaries for detection, provider-secret injection, Jupyter routing, scheduler identity, GPU visibility, and packaged runtime wiring.
They are operator inputs rather than the normal researcher project-selection mechanism.

`HEARTWOOD_CHECKPOINT_SIGNER_REGISTRY` is an operator-only override for one absolute deployment registry path when the standard system location is unavailable.
Heartwood otherwise checks `/etc/heartwood/checkpoint-signers.toml` and then the explicit workstation fallback at `~/.config/heartwood/checkpoint-signers.toml`.
Registry scopes are not merged.

Common examples include platform markers, `GOOGLE_PROJECT`, `CLUSTER_NAME`, Slurm variables, CUDA visibility, and provider credential bindings.
Do not add these to shell history or documentation with real secret values.

### Skill Source Registries

Signed Skill sources are deployment configuration rather than project state.
Heartwood checks `/etc/heartwood/skill-sources.toml` and then `~/.config/heartwood/skill-sources.toml`; the first existing registry is authoritative and registries are not merged.
`HEARTWOOD_SKILL_SOURCES_FILE` may select one absolute registry path for packaged or test environments.

A connected source uses HTTPS endpoints and a separately provisioned TUF root:

```toml
schema_version = "heartwood.skill-sources.v1"

[[sources]]
id = "institution"
kind = "remote"
trusted-root = "/etc/heartwood/trust/institution-skills-root.json"
metadata-url = "https://skills.example.edu/metadata/"
targets-url = "https://skills.example.edu/targets/"
```

An offline deployment points to a complete transferred repository:

```toml
schema_version = "heartwood.skill-sources.v1"

[[sources]]
id = "institution-offline"
kind = "offline"
trusted-root = "/approved-media/skills/metadata/1.root.json"
repository = "/approved-media/skills"
```

The trusted root must arrive through an independently trusted deployment channel.
Remote URLs must use HTTPS and cannot contain credentials, query strings, or fragments.
Heartwood verifies signatures, metadata versions and expiry, target length and digest, catalog policy, the complete archive manifest, and the extracted Agent Skill before installation.

Controlled-data approval is optional deployment evidence tied to exact tree digests:

```toml
controlled-data-approved-digests = [
  "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
]
```

Do not copy this example digest.
Repository review alone never populates this list.
An unrestricted marketplace, mutable branch, or unauthenticated archive URL is not a trusted Heartwood Skill source.

## Concurrency

Configuration updates are serialized across concurrent Heartwood processes.
Each active session also has an enforced writer lease.
Stop the process that owns a session before continuing it from another interface, or use separate session identifiers for simultaneous work.
Do not delete internal lock, command-receipt, or recovery-journal files.

## Audit Artifacts

Session audit logs and generated exports remain private project state.
An authoritative checkpoint, deployment signer registry, signer credential, private signing key, and independently trusted public key must resolve outside the project.
Use deployment records storage for checkpoint retention; `.heartwood/` is not an authoritative archive.

See [Audit Checkpoints and Retention](../operate/audit-checkpoints.md) for signing, verification, and key-management responsibilities.
