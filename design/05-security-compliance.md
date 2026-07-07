# 05 — Security and compliance

## Threat model

heartwood defends against: PHI exfiltration (via a model call, a skill script, a tool, a log, or a dependency fetch); malicious or careless community skills (a large fraction of public skills carry security flaws); prompt injection (direct and indirect); and silent incorrectness (a plausible-but-wrong analysis presented authoritatively).

## In-boundary by default (the load-bearing control)

- The network posture is **deny-egress**. Only the configured in-perimeter model endpoint is reachable — enforced by the model policy layer and the platform's own egress controls.
- **Per-platform policy profiles** encode each dataset's data-use rules (e.g. All of Us: no individual-level egress; aggregate only, ≥20 count; no dissemination of models trained on participant data) and hard-block non-compliant actions.
- **Credentials are a per-platform allowlist**, not a blanket deny: the platforms inject data-access credentials via env vars/metadata, so exactly the sanctioned data-path credentials are allowed and nothing else.

## Skill trust (four enforced layers)

1. **Curation tiers** — only `verified` skills are pre-seeded into a PHI image; `community` requires an approval step; `experimental` is opt-in only.
2. **Signing and provenance** — Sigstore/SLSA attestations bind a skill version to its source; verified at **build time** (where transparency-log lookups work) **and at load time** (catching runtime/long-tail additions).
3. **Sandboxing** — skill `scripts/` run under OS-level isolation (bubblewrap) via non-overridable managed settings: sandbox on, fail-closed, empty network allowlist, credential-file reads denied. The hostname-based proxy is TLS-blind, so the sandbox is defense-in-depth paired with platform egress-deny — documented, not overclaimed.
4. **Static scan + pinning + approval UX** — a security scan gates community skills in CI and re-runs on a schedule; dependencies are pinned with no runtime installs; before first use of a non-verified skill a plain-language summary ("can read the dataset mount; cannot access network; last reviewed …") is shown for a single approve/reject, recorded to the audit log.

## PHI handling

- **Synthetic-only test fixtures** — recorded traces/fixtures use Synthea-style synthetic data; recording against live PHI is forbidden and a CI scrubber fails on PHI-shaped content.
- **Logs record destinations, not content** by default; prompt/response content logging is opt-in and stays on the in-perimeter workspace disk.
- **Air-gapped image** — all dependencies vendored and pre-staged in the platform's registry; no public PyPI at runtime.

## Clinical-correctness gate

A skill can be safe (signed, sandboxed) yet clinically wrong. The `verified` tier therefore requires **two independent, both-mandatory reviews** — security and clinical/statistical — tracked separately.

## Compliance kit

Per platform, heartwood ships a copy-paste kit that turns per-site review into an artifact hand-off: pre-filled IRB / security-review language, a data-flow diagram, the policy-profile statement, and the egress-attestation report ([06](06-observability-audit.md)). The kit is validated against real institutional reviews before release.

## Governance

The initial maintenance home is the **Stanford Schmiedmayer Lab**, with a written `GOVERNANCE.md` and a succession plan. Later phases add external maintainers and, once there is traction, a neutral home (e.g. the Linux Foundation Agentic AI Foundation or a bio-consortium). A realistic funding model underwrites long-term maintenance.
