# 07 — Testing and evaluation

Verification is the guardrail against low-quality generated output, and it runs without touching PHI. Quality gates block merges and releases; they never slow a researcher's live session.

## Layers

1. **Unit tests of pure-code paths.** The detector, policy-layer egress decisions, the verification gate, signature checks, and adapters are pure code with no model call, tested against synthetic fixtures of each platform + dataset. This covers the majority of the safety-critical logic, fully offline.
2. **Record / replay of agent trajectories.** Every model and tool call in a run is recorded and replayed deterministically for near-zero-cost CI. Trajectory evals check the end state; single-step evals check each decision. Replaying old traces against a new model/prompt/harness surfaces regressions. **All fixtures are synthetic** (Synthea-style); recording against live PHI is forbidden.
3. **Model-graded and metric evals, run locally.** Inspect AI (agent eval plans), promptfoo (CI-native, injection scanning), and DeepEval (pytest-like, local judges — important when the judge must stay in-perimeter).
4. **Skill evals.** Every skill ships evals; passing is required for the `verified` tier (`skillgrade`/SkillsBench-style). A skill PR that drops pass-rate below threshold fails CI. This doubles as the surface for the clinical-correctness check.
5. **Interface contract tests.** CLI commands, interactive-session transcripts, and notebook view models are generated from the same synthetic session events and compared against stable snapshots.
6. **Coding-ability benchmarks.** A pinned subset of SWE-bench Verified, Terminal-Bench, and LiveCodeBench, plus an in-house synthetic-OMOP benchmark, tracks whether a model or harness change helped. Harness commits are pinned so numbers stay comparable.

## Model-capability gate

Each supported model is benchmarked and assigned a tier the policy layer enforces at runtime:

- **Tier 1 (autonomous)** — an in-perimeter frontier model above threshold; full autonomy.
- **Tier 2 (supervised)** — capped autonomous tool-loop depth, forced confirmation.
- **Tier 3 (experimental)** — interactive/single-step only; refuses autonomous runs.

Local open-weight models typically land in Tier 2/3. Autonomy is decided by measured capability, not by configuration.

## CI pipeline

Lint + type-check + pure-code unit tests → CLI/notebook interface contract tests → skill schema/signature/security-scan for changed skills → replay regression suite (synthetic) → skill evals → build the air-gapped image, verify signatures, generate an SBOM, sign artifacts. Nightly: pinned benchmarks, full-registry re-scan, and capability-tier re-benchmarks.
