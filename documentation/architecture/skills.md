<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Skill Trust and Distribution

Heartwood uses the standard Agent Skills directory format and the public OpenHands Skill loader.
It adds a distribution and activation boundary for research environments; it does not create a second Skill format or agent runtime.

## Ownership

| Owner | Responsibility |
|---|---|
| `heartwood-skills` | Curated Skill directories, strict OpenHands validation, complete-tree policy, deterministic archives, and catalog generation |
| Skill source operator | TUF root, signing roles, metadata expiry, target publication, revocation, and source availability |
| Heartwood deployment | Trusted roots, permitted sources, exact controlled-data approvals, platform policy, and network reachability |
| Heartwood gateway | Source refresh, verification, inspection, explicit approval, atomic project activation, audit records, and the shared interface projection |
| OpenHands | Skill loading, conversation behavior, and coding tools after Heartwood supplies verified active directories |

The Heartwood release pins one exact `heartwood-skills` Git revision as a submodule.
Packaging verifies that the initialized directory is clean and matches that gitlink, then archives that revision into native and container artifacts.
Bundled Skills are governed by that Heartwood release; withdrawing bundled content requires a new Heartwood release.
Signed catalog revocations govern catalog-installed content and do not silently rewrite release-bundled content.

OpenHands also provides installed-Skill and marketplace APIs for its general-purpose runtime.
Heartwood does not use those APIs as a second installation registry because they do not carry deployment TUF roots, exact-digest controlled-data approvals, signed revocation state, or project-scoped audit evidence.
Heartwood instead verifies and activates a Skill once in its gateway-owned project store, then passes only the verified active directories to the public OpenHands loader.
Installation decisions and lifecycle results use Heartwood's existing recoverable, scrubbed, hash-chained audit log rather than a Skill-specific logging format.

## Signed Source Flow

```mermaid
flowchart LR
    Registry["Deployment source registry"] --> Root["Independent trusted TUF root"]
    Root --> Metadata["Signed and unexpired metadata"]
    Metadata --> Catalog["Catalog target"]
    Catalog --> Archive["Immutable Skill archive"]
    Archive --> Verify["Complete-tree and OpenHands verification"]
    Verify --> Review["Researcher review of exact digest"]
    Review --> Store["Atomic content-addressed project store"]
    Store --> Gateway["Gateway Skill projection"]
    Gateway --> Interfaces["CLI, browser, and notebook"]
    Gateway --> OpenHands["OpenHands active Skill directories"]
```

Heartwood refreshes signed metadata again during installation and compares the current tree digest with the digest presented for approval.
It refuses expired metadata, missing targets, substitutions, archive-manifest differences, unsafe paths, symbolic or hard links, special files, unsupported tools, undeclared network requirements, incompatible platforms, and revoked content.

Installed catalog artifacts are addressed by the complete tree SHA-256 digest.
The activation index records the source identifier, full source commit, catalog target, version, review status, and revocation status.
One project-scoped native lock serializes source refresh, download, activation, removal, and runtime revalidation across CLI and browser processes.
Heartwood revalidates the signed source, installed tree, and matching activation event in the verified audit chain before exposing an active catalog Skill to OpenHands.

## Offline Use

An offline source is a transferred TUF repository containing metadata and targets plus its independently obtained trusted root.
The same Python-TUF client performs signature, rollback, freeze, length, and hash checks without network access.
The offline path is not a bypass for unsigned directories.

An operator can still install a complete local directory through the explicit advanced path.
Heartwood labels that content **Local and unreviewed**, copies it without following links, and binds approval to the exact complete-tree digest.

## Separate Decisions

Repository review answers whether the package met the curated source's code, policy, and test requirements.
Installation approval answers whether a researcher accepted the exact package and declared permissions for one project.
Controlled-data approval is deployment evidence for one exact digest.

None of these decisions grants a tool, expands the project boundary, supplies a credential, or overrides action confirmation.
The active OpenHands tool set and deployment policy remain authoritative.

## Interface Contract

The gateway owns one `SkillSummary` projection for bundled, available, installed, revoked, unsupported, and local-candidate content, including declared tools, network use, data access, and dataset types.
The CLI, REST API, browser, and notebook bridge render that projection and submit the same inspect, refresh, install, and remove operations.
They do not parse catalogs, validate files, or infer trust independently.
