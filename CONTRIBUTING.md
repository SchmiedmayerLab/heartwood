<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Contributing to Heartwood

Heartwood accepts focused changes that preserve its auditable, platform-aware coding-agent contract for biomedical research. Public source, tests, examples, screenshots, and logs must contain synthetic data only.

## Prepare the Repository

Heartwood requires Python 3.12, `uv`, Node.js for the web interface, and Docker for image validation.

```bash
uv sync --locked --all-extras --all-groups --python 3.12
uv run heartwood --version
```

Read [AGENTS.md](AGENTS.md) for repository rules and the [documentation home](documentation/index.md) for the current operational and technical contracts. Run Heartwood from the repository root only when the repository itself should be the active project. Its local `.heartwood/` directory is private runtime state and must not be committed.

## Make a Change

- Keep the change within the existing component and adapter boundaries described in [System Architecture](documentation/architecture/system.md).
- Reuse OpenHands and the existing platform, provider, policy, session, and UI abstractions instead of creating a parallel execution path.
- Add or update tests for changed behavior, including failure and recovery paths.
- Update the relevant user guidance or durable rationale in `documentation/` when its contract changes.
- Track planned implementation and acceptance criteria in GitHub Issues rather than release documentation.
- Add new acronyms and specialized terms to the [Glossary](documentation/reference/glossary.md).

## Validate the Change

Run the checks relevant to the edited surface. The complete local baseline is:

```bash
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy packages
npm --prefix packages/webui test
npm --prefix packages/webui run build
uv run --no-sync zensical build --clean --strict
```

Container, installer, release, security, and platform changes also require their corresponding workflow or smoke-test contracts. Do not weaken a required check to make a change pass.

## Open a Pull Request

Use the repository pull-request template. Describe the user-visible behavior, validation performed, security or deployment implications, and documentation changes. Keep commits reviewable and do not include credentials, model weights, participant-level data, generated runtime state, or private platform evidence.

By contributing, you agree that your contribution is licensed under the repository's [MIT License](LICENSE).
