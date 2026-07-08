<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# heartwood

*Working name.* An open-source, compliance-first coding assistant for sensitive biomedical research data.

heartwood is a self-contained Docker image that runs inside a trusted research environment, next to controlled data. It detects the platform and dataset, proposes vetted analysis skills, and turns a plain-language research question entered through an agentic CLI or notebook interface into reproducible code, aggregate results, and an auditable record.

Participant-level data stays inside the platform boundary. Model calls go only to approved in-boundary endpoints or local models, and every meaningful decision is logged for review.

## How it works

1. **Launch** the image in Terra / All of Us, Seven Bridges, DNAnexus, or a generic Linux/Jupyter environment.
2. **Start** a CLI session, or attach a notebook widget view to the same session.
3. The detector fingerprints the platform and data source, then **proposes** matching skills and scoped tools for human confirmation.
4. The agent writes and runs Python in a sandbox through platform and data adapters.
5. The policy layer routes model calls to a local or private endpoint, blocks public egress by default, and records each decision.
6. The user receives aggregate results, an activity view, a tamper-evident audit log, and an egress attestation.

## Architecture at a glance

- **Agent core** — OpenHands SDK behind a stable facade: agent loop, sandbox, MCP client, skill loading, and event log.
- **Platform layer** — narrow adapters for platform detection, model routing, data access, and skill registries.
- **Detection and skills** — deterministic environment/data fingerprints plus curated `SKILL.md` packages with `heartwood.*` metadata, signatures, and evals.
- **Policy and compliance** — deny-egress defaults, per-platform data-use profiles, aggregate-export guards, and review-ready compliance artifacts.
- **Audit and evaluation** — hash-chained logs, scrubbed exports, synthetic replay, and capability gates for supported models.
- **Interaction surfaces** — CLI as the primary product and test interface; notebook API and widgets as a secondary presentation layer over the same sessions.
- **Implementation** — Python for core services, adapters, CLI, and notebook integration; container-first packaging.

## Approach

Reuse the agent loop; own the biomedical, platform, and compliance layer. Adopt standards (`SKILL.md`, MCP, GA4GH, Sigstore) instead of inventing private formats. Keep the core platform-agnostic, bridge each environment with thin adapters, and ship an air-gapped image that can be reviewed once and reused.

## Try it

Heartwood is early. The commands below run today from a checkout of this repository; the full end-to-end reference workflow (detect → confirm skill → cohort → QC → baseline → attestation) is being built phase by phase — see the [implementation plan](design/09-implementation-plan.md).

The only prerequisites are [`uv`](https://docs.astral.sh/uv/) and `git`; `uv` fetches the pinned Python toolchain and dependencies.

```bash
uv sync                        # create the environment (from a checkout of this repo)
uv run heartwood --version     # heartwood 0.0.0
uv run heartwood detect        # inspect the environment and propose — nothing runs
```

`heartwood detect` runs the deterministic, propose-not-commit platform probe — no model call and no data access — and prints what it found and how confident it is. This is the "detection proposes, a human confirms" principle the whole system is built on:

```text
Heartwood — environment detection

This is a proposal only. Nothing loads or runs without your confirmation.

Platform: generic (confidence 1.00)
  - no managed-platform environment markers detected

Dataset detection and skill proposals are not implemented yet (see design/04-skills.md).
```

On a managed platform the probe reports it from environment markers alone — for example `Platform: dnanexus (confidence 0.90)` when it sees `DX_*` variables, or `terra` from `WORKSPACE_*` / `GOOGLE_PROJECT`.

### The full workflow (roadmap)

Once the session contract, skills, policy layer, and image land, a researcher will drive one guided flow from the CLI — or from a notebook widget attached to the same session:

```text
$ heartwood chat
> summarize the diabetes cohort by age band
detected OMOP (0.82) → propose skill `omop-cohort-summary`   [confirm] [pick] [why]
> confirm
… wrote and ran a query in the sandbox · called the in-perimeter model …
returned aggregate counts (every cell ≥ 20 participants)

$ heartwood audit export     # scrubbed audit bundle + egress attestation for review
```

Participant-level data never leaves the boundary; only reviewed, aggregate results do. See the [reference workflow](design/01-overview.md) and the [security model](design/05-security-compliance.md).

### Developing

Every check below runs in CI and can be run locally; all must pass before review:

```bash
uv run ruff format --check .   # formatting
uv run ruff check .            # lint
uv run mypy packages           # strict static types
uv run pytest                  # tests + coverage gate
reuse lint                     # REUSE / SPDX licensing
```

See the [Schmiedmayer Lab contributing guidelines](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) for the contribution process.

## Documentation

| Doc | Contents |
|-----|----------|
| [01 · Overview](design/01-overview.md) | What it is, personas, scope |
| [02 · Platforms](design/02-platforms.md) | Target environments, embedding, in-boundary models, data-use policy |
| [03 · Architecture](design/03-architecture.md) | Core, adapter SPI, model policy, data flow |
| [04 · Skills](design/04-skills.md) | `SKILL.md`, auto-detection, sharing, skill trust |
| [05 · Security & compliance](design/05-security-compliance.md) | In-boundary enforcement, PHI, compliance kit, governance |
| [06 · Observability & audit](design/06-observability-audit.md) | Audit trail, tamper-evidence, feedback loop |
| [07 · Testing & evaluation](design/07-testing-eval.md) | Record/replay, evals, capability gate |
| [08 · Development](design/08-development.md) | Languages, linting, licensing, CI |
| [09 · Implementation plan](design/09-implementation-plan.md) | Phased delivery, repo layout, open questions |

## Contributing

Contributions to this project are welcome. Please make sure to read the [contribution guide](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [Contributor Covenant Code of Conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first.

Because Heartwood runs next to controlled data, contributors must never include PHI, credentials, or live-platform identifiers in issues, pull requests, tests, or fixtures; all fixtures are synthetic. Project-direction changes update the relevant [`design/`](design) document first, and security- or compliance-relevant claims must be backed by tests, audit records, or a documented limitation.

## License

This project is licensed under the MIT License. See [Licenses](LICENSES) for more information.

## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-light.png#gh-light-mode-only)
![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-dark.png#gh-dark-mode-only)
