<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Heartwood

[![GitHub Release](https://img.shields.io/github/v/release/SchmiedmayerLab/heartwood?display_name=tag&sort=semver)](https://github.com/SchmiedmayerLab/heartwood/releases/latest)
[![Python](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/python.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/python.yml)
[![Validate](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/validate.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/validate.yml)
[![CodeQL](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/codeql.yml)
[![Secret Scan](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/gitleaks.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/gitleaks.yml)
[![Dependency Review](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/dependency-review.yml/badge.svg?branch=main)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/dependency-review.yml)
![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)

Heartwood is an auditable coding-agent environment for sensitive biomedical research data. It runs close to controlled data and adds repository-verified biomedical Skills, model-route policy, platform detection, scrubbed audit records, and researcher-focused CLI, notebook, and web interfaces around OpenHands.

Participant-level data must remain within an institution-approved deployment boundary enforced by platform identity, storage, and network controls. Source control, examples, documentation, and CI use synthetic fixtures only.

## Product Contract

- OpenHands owns the agent loop, coding tools, action-risk analysis, action confirmation, conversation state, LiteLLM provider compatibility, and native `SKILL.md` loading.
- Heartwood owns biomedical Skill curation, deployment route authorization, platform and dataset detection, audit and attestation records, and safe export controls.
- The CLI and web UI provide the same conversation workflow: submit a task, inspect messages and proposed action sets, allow or reject a pending OpenHands action set, pause or resume, replay, and export the audit trail.
- Model connections discover provider, platform, and local catalogs; the selected entry becomes the existing non-secret OpenHands model profile. Secret values are read from environment variables, mounted files, managed identity, or a web-submitted token retained only by the running gateway.
- Generic and Terra-derived images include an optional CPU llama.cpp runtime but no model weights. Local weights are explicitly downloaded to or mounted from persistent storage.
- A model connection is not a compliance claim. The deploying institution remains responsible for business associate agreements, covered-service configuration, retention settings, identity, region, and network controls.

See [Architecture](design/03-architecture.md), [Security And Compliance](design/05-security-compliance.md), and [Platform Support](docs/platform-support.md) for the complete contract and current evidence.

## Capabilities And Limitations

Heartwood provides an OpenHands SDK runtime, two researcher-selectable action-confirmation modes, model connections and advanced profiles, reviewed local-artifact and multi-file snapshot downloads, repository-verified Skill loading, a CLI, notebook bridge, conversation-first web UI, gateway-owned session lifecycle, content-minimized audit export, a multi-platform generic image, and a Terra-derived image. Public examples and automated validation use synthetic data.

The default runtime uses the generic platform policy and synthetic OMOP data-source fixture. File-backed sessions support one active writer, and the repository does not confer institutional approval for any deployment. Boundary and workflow labels require typed gateway evidence; absent evidence remains unknown rather than being inferred by the web UI.

See [Platform Support](docs/platform-support.md) for platform-specific evidence and limitations. All of Us, AnVIL, Seven Bridges, Velsera, DNAnexus, and UK Biobank Research Analysis Platform are design targets rather than supported platforms.

Tagged releases publish a verified native bundle for environments where a platform image is not appropriate. The [latest GitHub Release](https://github.com/SchmiedmayerLab/heartwood/releases/latest) contains the installer, checksum manifest, source bundle, generated notes, and attestations. Install a selected immutable release with:

```bash
HEARTWOOD_VERSION=0.1.1
curl --fail --location --remote-name \
  "https://github.com/SchmiedmayerLab/heartwood/releases/download/${HEARTWOOD_VERSION}/heartwood-installer"
chmod +x heartwood-installer
./heartwood-installer --root /persistent/project/heartwood --version "${HEARTWOOD_VERSION}"
export PATH="/persistent/project/heartwood/bin:${PATH}"
heartwood
```

The bare `heartwood` command inspects the persisted setup and detected platform, then opens the next useful step: initial setup, Carina compute guidance, recovery guidance, or the interactive conversation. `heartwood doctor` remains available as a read-only diagnostic. Maintainers create releases through the protected [release workflow](docs/releases.md), which publishes only after all required checks pass for the exact `main` commit and the designated maintainer approves the protected environment.

`heartwood launch --dry-run` reports the detected platform, storage, model, and compute plan without changing state. On Carina it discovers an available GPU partition and asks before invoking Slurm; Terra and generic containers use their already-provisioned compute. See [Set Up Heartwood On Carina](docs/carina-cli.md) for the synthetic native GPU workflow and [Using Heartwood](docs/using-heartwood.md) for the shared interaction model.

## Researcher Experience

![Heartwood synthetic reference analysis](docs/assets/web-reference-analysis.png)

The [Researcher Web Interface](docs/web-interface.md) explains model setup, the synthetic reference analysis, action review, audit export, CLI parity, notebook use, and reproducible screenshot generation. The image is generated by the real browser-to-gateway-to-OpenHands reference test with synthetic data and a deterministic loopback model; it is not a model-quality or live-platform claim.

## Local Development

Install [`uv`](https://docs.astral.sh/uv/) and use Python 3.12 for the OpenHands runtime:

```bash
uv sync --all-extras --python 3.12
uv run heartwood --version
uv run heartwood detect
uv run heartwood models list
```

Start a local OpenAI-compatible service on `127.0.0.1:8765` that implements model listing and chat completions, then select one of the identifiers it reports:

```bash
uv run heartwood models refresh local
uv run heartwood models connect local <model-id>
uv run heartwood chat
```

For a hosted provider, expose its credential through the provider's standard environment variable and select an identifier returned by the official provider API:

```bash
export OPENAI_API_KEY="..."
uv run heartwood models refresh openai
uv run heartwood models connect openai <model-id>
```

Heartwood authorizes the exact catalog endpoint before discovery and separately denies a turn until the deployment policy supplied through `HEARTWOOD_POLICY_PROFILE` authorizes the selected profile's normalized completion endpoint, capability tier, action-confirmation mode, and non-secret credential reference. A custom `base_url` must share those endpoints' origin. Platform network controls remain authoritative for the provider destination and must independently restrict actual traffic. Credential allowlist entries are environment-variable names, absolute mounted-file paths, or `managed-identity`; secret values never enter policy. See [Model Connections](docs/model-connections.md) for platform and custom configuration.

Build and serve the web UI:

```bash
cd packages/webui
npm ci
npm run build
cd ../..
uv run heartwood serve --web-root packages/webui/dist
```

Open `http://127.0.0.1:8767/`. The conversation is primary; model setup, optional reviewed local downloads, policy validation, activity, and audit export are available from secondary controls. Model setup groups installed local models, platform-provided research services, OpenAI, Anthropic, and a custom OpenAI-compatible API. Raw execution profiles remain under **More options** for operators and compatibility.

Action confirmation defaults to **Ask Every Time**. Generic synthetic development may select the only automatic mode from the CLI or the same two-choice web settings control:

```bash
uv run heartwood actions
uv run heartwood actions set auto-approve-low-risk
```

**Auto-Approve Low Risk** delegates classification and confirmation to OpenHands: low-risk actions execute automatically, while medium-, high-, and unknown-risk action sets still require **Allow all once** or **Reject all**. OpenHands approves or rejects all actions from one confirmation stop as a set; Heartwood displays every member and does not claim unsupported per-action execution. Managed deployment policy permits confirmation modes explicitly and defaults to `always-confirm`; Heartwood does not expose OpenHands `NeverConfirm` in researcher settings.

Repository-verified bundled Skills load automatically. To add a mounted community or experimental extension, inspect its permissions and approve one installation through the CLI or Skills panel:

```bash
uv run heartwood skills list
uv run heartwood skills inspect /path/to/mounted-skill
uv run heartwood skills install /path/to/mounted-skill --approve
```

Installation verifies metadata, allowed tools, network posture, entrypoint, path confinement, and symbolic-link absence, then records the trust decision and copies the Skill atomically into persistent state. Normal Skill activation does not prompt again.

## Container Runtime

The immutable `0.1.1` generic image supports `linux/amd64` and `linux/arm64`. It contains no credentials and no model weights. The `edge` tag follows validated `main` builds and is intended for development rather than reproducible deployment.

```bash
docker pull ghcr.io/schmiedmayerlab/heartwood:0.1.1
docker run --rm -p 127.0.0.1:8767:8767 \
  -v heartwood-state:/home/heartwood/.local/share/heartwood \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:0.1.1 \
  bash images/generic/scripts/start_demo_stack.sh
```

Open `http://127.0.0.1:8767/` and choose an authorized model connection. Pass durable provider credentials through runtime environment or secret mounts, never image build arguments.

The image includes a pinned CPU `llama-server`. List and explicitly download reviewed demonstration artifacts into the mounted cache with:

```bash
docker run --rm \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:0.1.1 heartwood models artifacts
docker run --rm \
  -v heartwood-models:/home/heartwood/.cache/heartwood/models \
  ghcr.io/schmiedmayerlab/heartwood:0.1.1 \
  heartwood models download qwen25-7b-instruct-q4_k_m
```

The download command prints the verified path. `qwen25-7b-instruct-q4_k_m` is the reviewed local agent demonstration artifact because the pinned llama.cpp runtime supports its native tool-call format; `qwen25-coder-7b-instruct-q4_k_m` remains available for coding-output experiments but is not the default OpenHands tool-use acceptance model. Neither artifact has a biomedical, production, or benchmark-backed quality claim. Set `HEARTWOOD_LOCAL_MODEL_PATH` to the selected path and `HEARTWOOD_DEMO_START_LOCAL_RUNTIME=1` when starting the demo stack, then choose the model reported by the Local connection. Existing Ollama, vLLM, SGLang, llama.cpp, or other OpenAI-compatible services can be selected without using Heartwood's artifact catalog.

From a checkout, the isolated CI path builds the same no-weight image and uses a deterministic loopback model fixture with real OpenHands SDK orchestration:

```bash
docker compose -f images/generic/compose.yaml run --rm --build heartwood
```

That deterministic no-network gate exercises model routing, OpenHands orchestration, both approval modes, Skills, audit, CLI, web-support, and notebook contracts without claiming real model inference. The mounted capable-model acceptance command in [Getting Started With Local And Offline Models](docs/getting-started-offline.md) separately requires a native model tool call, successful terminal execution, the expected workspace file, and complete policy and audit events while container networking is disabled.

See [Container Images](docs/container-images.md) and [Getting Started With Local And Offline Models](docs/getting-started-offline.md).

## Terra

Release `0.1.1` publishes `ghcr.io/schmiedmayerlab/heartwood:0.1.1-terra`, a no-weight image derived from the pinned Terra Jupyter Python base. It preserves the Terra user, home, Jupyter entrypoint, kernel environment, and Leonardo route behavior while adding the same Heartwood application and Skills as the generic image. Terra requires this separate `linux/amd64` Docker schema-2 tag because Leonardo does not accept the generic multi-platform OCI index. The `edge-terra` tag remains the moving validated-main channel.

Use the [Terra Jupyter Demo](docs/terra-jupyter-demo.md) for the synthetic end-to-end workflow and the [Platform Image Extension Guide](docs/platform-images.md) for adding another platform base.

## Repository Structure

- [`design`](design) contains durable product, architecture, security, testing, and development decisions.
- [`docs`](docs) contains runnable setup and platform guides.
- [`packages/gateway`](packages/gateway) contains the OpenHands adapter, model profile and artifact settings, and shared HTTP/event gateway.
- [`packages/core-adapter`](packages/core-adapter) contains auditable session orchestration and the agent-backend contract.
- [`packages/cli`](packages/cli), [`packages/notebook`](packages/notebook), and [`packages/webui`](packages/webui) contain the interaction surfaces.
- [`packages/model-policy`](packages/model-policy), [`packages/audit`](packages/audit), and [`packages/compliance`](packages/compliance) contain deployment policy, tamper-evident audit, and evidence tooling.
- [`packages/skills`](packages/skills) and [`skills`](skills) contain Skill verification, packaging, tests, and the verified biomedical Skills.
- [`images`](images) contains the generic and platform-derived no-weight image definitions and integration smokes.
- [`fixtures`](fixtures) and [`evals`](evals) contain synthetic-only fixtures and replay evaluations.

## Development Checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy packages
uv run pytest
uvx reuse lint
uv run heartwood-fixtures fixtures skills evals
```

```bash
cd packages/webui
npm run format:check
npm run lint
npm run typecheck
npm run test
npm run build
npm run license:check
npm run test:e2e
```

Do not add protected health information, credentials, live-platform identifiers, or non-synthetic records to tests, fixtures, examples, issues, or pull requests.

## Documentation Structure

[Documentation](docs/README.md) defines the authority boundary between current operational guides, durable design rationale, and project planning.

| Document | Contents |
|---|---|
| [Documentation Index](docs/README.md) | Current operations, design rationale, publication, and status vocabulary |
| [Published Documentation](https://schmiedmayerlab.github.io/heartwood/) | Operational and design documentation from the latest published release |
| [Platform Support](docs/platform-support.md) | Current implementation, image, CI, and live-validation status |
| [Overview](design/01-overview.md) | Scope, users, and reference workflow |
| [Platforms](design/02-platforms.md) | Deployment boundaries and platform assumptions |
| [Architecture](design/03-architecture.md) | Ownership, contracts, and data flow |
| [Skills](design/04-skills.md) | `SKILL.md` packaging, trust, and activation |
| [Security And Compliance](design/05-security-compliance.md) | Data, model, action, and export controls |
| [Observability And Audit](design/06-observability-audit.md) | Audit trail and tamper evidence |
| [Testing And Evaluation](design/07-testing-eval.md) | Replay, integration, and capability gates |
| [Development](design/08-development.md) | Toolchain, CI, and supply chain |
| [Researcher Web Interface](docs/web-interface.md) | Shared session workflow, model setup, action review, audit, CLI parity, and notebook layout |
| [Using Heartwood](docs/using-heartwood.md) | Terminal, web, and notebook interaction; action-set review; replay; audit; and persistence |
| [Container Images](docs/container-images.md) | Tags, model storage, providers, and CI |
| [Platform Image Extension Guide](docs/platform-images.md) | Thin platform image mechanism |
| [Terra Jupyter Demo](docs/terra-jupyter-demo.md) | Synthetic Terra workflow |
| [Set Up Heartwood On Carina](docs/carina-cli.md) | Synthetic native GPU and Stanford AI API Gateway workflow |

## Project Planning

Planned implementation and acceptance criteria are tracked in [GitHub Issues](https://github.com/SchmiedmayerLab/heartwood/issues) and organized by delivery status in the [Heartwood Project](https://github.com/orgs/SchmiedmayerLab/projects/2). Published documentation describes current behavior and durable design decisions; an open issue is not a capability or support claim.

## Contributing

Read the [contribution guide](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and [Contributor Covenant Code of Conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md). Project-direction changes update the relevant design document first, and security or compliance claims require test evidence or a documented limitation.

## License

Heartwood is licensed under the MIT License. See [LICENSE](LICENSE).

## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-light.png#gh-light-mode-only)
![Stanford and Stanford Medicine logos](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/stanford-footer-dark.png#gh-dark-mode-only)
