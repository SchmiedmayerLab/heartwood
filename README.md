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

## Status

Pre-implementation design. MIT-licensed. Maintained by the Stanford Schmiedmayer Lab. Domain acronyms are tracked in [ACRONYMS.md](ACRONYMS.md).
