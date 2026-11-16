<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="documentation/assets/brand/heartwood-hero-dark.svg">
    <img alt="Heartwood" src="documentation/assets/brand/heartwood-hero-light.svg">
  </picture>
</h1>

[![Build and Test](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/main-validation.yml/badge.svg)](https://github.com/SchmiedmayerLab/heartwood/actions/workflows/main-validation.yml)
[![REUSE status](https://api.reuse.software/badge/github.com/SchmiedmayerLab/heartwood)](https://api.reuse.software/info/github.com/SchmiedmayerLab/heartwood)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.md)
[![Release](https://img.shields.io/github/v/release/SchmiedmayerLab/heartwood?display_name=tag&include_prereleases&sort=semver)](https://github.com/SchmiedmayerLab/heartwood/releases)

[Stable Documentation](https://schmiedmayerlab.github.io/heartwood/) · [Prerelease Documentation](https://schmiedmayerlab.github.io/heartwood/preview/)

Heartwood is an open-source research agent system for biomedical research environments.
A researcher describes work on a project folder in ordinary language, and an OpenHands agent proposes commands and file changes.
The Heartwood gateway decides which proposals wait for the researcher, records every request, decision and result, and keeps a hash-chained audit history that can be exported and signed.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="documentation/assets/screenshots/browser-action-review-dark.png">
  <img alt="Heartwood browser interface showing a grouped action review" src="documentation/assets/screenshots/browser-action-review-light.png">
</picture>

## Components

- The gateway owns project state, policy, action review, authorization of operations outside the agent, session records and audit.
- The agent loop and coding tools come from OpenHands and run either in the Heartwood process or as a complete worker inside an OpenShell sandbox.
- The terminal (`heartwood`), browser (`heartwood --interface web`) and notebook (`NotebookSession()`) interfaces present the same gateway contract, so a session and its review decisions look the same in each.
- The operator's deployment manifest binds a project to protected state outside the research folder and sets the policy, the permitted review modes, the worker profile and the audit evidence settings.
- `heartwood audit checkpoint` writes a signed audit export with deployment and retention metadata outside the project, and `heartwood audit verify-checkpoint` checks it against a trusted public key; with managed evidence, the gateway checkpoints every approval and rejection.
- Research Skills, bounded research specialists and staged research workflows run inside the same session, review and records.

## Two Ways to Run It

### Local Launch

A researcher runs `heartwood` in a project folder on a workstation, in a container, on Terra or on Stanford Carina.
Heartwood keeps configuration, sessions and audit records in `.heartwood/` inside that folder.
Every action set waits for the researcher, unless they select **Review Changes**, which allows file views and plan updates without a decision when the model route needs no credential.
Agent tools run with the researcher's own permissions: an allowed command can read anything that user can read and can change `.heartwood/`, so a local launch is a reviewed assistant, not an isolation boundary.

### Operator Deployment

An operator starts `heartwood gateway serve --deployment /absolute/path/project.toml` with a manifest whose `[worker]` table runs each session in an OpenShell sandbox on an operator-controlled Linux Docker host.
The worker receives selected read-only inputs, verified Skills and a private workspace; the protected state, provider credentials, Docker socket and signing keys stay outside it.
The gateway journals and fences each worker attempt, accepts a worker response only when its settings match what the gateway sent, inspects the container's confinement, and deletes the sandbox when it revokes an attempt.
Under **Review Outside Effects**, terminal commands and file edits inside the worker can run without a per-action decision.
Operations outside the worker go through a service adapter that an embedding launcher supplies, and each operation and the release of its result needs a researcher decision bound to the exact content reviewed.
The platform keeps authority over identity, storage, network controls, compute and inference under the [platform contract](https://schmiedmayerlab.github.io/heartwood/operate/platform-contract/).
[Security Limits](https://schmiedmayerlab.github.io/heartwood/operate/security/#security-limits) lists what these controls do not cover.

## First Session

With Docker on macOS or Linux:

```bash
mkdir heartwood-demo
cd heartwood-demo

docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.5.0 \
  heartwood --interface web --host 0.0.0.0 --host-loopback-publication
```

Open the launch link the container prints; the page continues at [http://127.0.0.1:8767/](http://127.0.0.1:8767/).
Confirm the project, choose a model connection, and ask for a small task.
For the terminal, replace the last line with `heartwood`.
The image contains no model weights or credentials.

Use synthetic or non-sensitive files.

## Documentation

- [Your First Project](https://schmiedmayerlab.github.io/heartwood/start/) walks through the first reviewed action.
- [Work With Heartwood](https://schmiedmayerlab.github.io/heartwood/use/) covers the interfaces, review modes, Skills, specialists and research workflows.
- [Choose a Platform](https://schmiedmayerlab.github.io/heartwood/platforms/) covers containers, native Linux, Terra and Stanford Carina.
- [Deploy Heartwood](https://schmiedmayerlab.github.io/heartwood/operate/) covers the deployment manifest, the isolated worker and deployment validation.
- [Security and Controlled Data](https://schmiedmayerlab.github.io/heartwood/operate/security/) describes the threat model and what the sandbox and the gateway enforce.
- [How Heartwood Works](https://schmiedmayerlab.github.io/heartwood/architecture/) explains the architecture.

## Contributing

Contributions to this project are welcome. Please make sure to read the [contribution guidelines](https://github.com/SchmiedmayerLab/.github/blob/main/CONTRIBUTING.md) and the [contributor covenant code of conduct](https://github.com/SchmiedmayerLab/.github/blob/main/CODE_OF_CONDUCT.md) first. You can find a list of contributors in the [CONTRIBUTORS.md](CONTRIBUTORS.md) file.

## License

This project is licensed under the MIT License. See [LICENSE.md](LICENSE.md) for more information.

## Citation

If you use this software, please cite it using the metadata in [CITATION.cff](CITATION.cff), which GitHub surfaces through the [*Cite this repository*](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-citation-files) button.

## Our Research

For more information, visit the [Schmiedmayer Lab GitHub organization](https://github.com/SchmiedmayerLab).

![Schmiedmayer Lab](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/footer-light.png#gh-light-mode-only)
![Schmiedmayer Lab](https://raw.githubusercontent.com/SchmiedmayerLab/.github/main/assets/footer-dark.png#gh-dark-mode-only)
