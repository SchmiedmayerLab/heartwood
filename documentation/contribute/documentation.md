<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Documentation Guide

The canonical published source is `documentation/`.
Zensical builds that tree directly; there is no copied staging tree or duplicate `docs` and `design` source hierarchy.

## Content Types

- **Get Started** teaches one complete first success.
- **Work With Heartwood**, **Models**, and **Platforms** solve researcher tasks.
- **Operate Heartwood** supports deployment and security reviewers.
- **How Heartwood Works** explains durable architecture and rationale.
- **Reference** provides complete, exact, scannable facts.
- **Contribute** documents repository work and releases.

Do not mix implementation progress, validation transcripts, future features, or issue discussions into user documentation.
Track planned work in GitHub Issues and keep run-specific evidence in CI artifacts or pull requests.

## Writing Style

- begin with the user's outcome and prerequisites;
- explain a path before asking the reader to type it;
- use semantic line breaks with one complete sentence per source line;
- use numbered steps for ordered work and tables only for genuine comparison;
- place warnings before risky data, cost, credential, or destructive operations;
- include an expected result and recovery route for important commands;
- define specialized terms in the glossary;
- use current product vocabulary consistently; and
- make conservative, evidence-backed capability and security claims.

Avoid conversational drafting notes, release-relative narration, unexplained acronyms, guessed URLs, placeholder screenshots, and commands that depend on files the guide did not create.

## Link to Published Documentation

Repository entry points such as `README.md` link user guidance to the stable documentation root at `https://schmiedmayerlab.github.io/heartwood/`.
They may also offer `https://schmiedmayerlab.github.io/heartwood/preview/` as an explicitly labeled prerelease option, but should not describe preview content as an unreleased future version.
Release notes and support records may link an immutable `/<version>/` documentation path when exact historical behavior matters.
Do not add a deployed deep link until that route exists in the referenced release channel; use the channel root while a new information architecture is awaiting publication.

Links between pages inside `documentation/` remain relative so Zensical and Mike preserve the active stable, preview, or immutable version prefix.

## Screenshots

Screenshots must use synthetic data, a current release interface, a desktop viewport of at least 1280 × 800, readable text, and no credentials or personal identifiers.
Generate the canonical browser images through the deterministic web smoke script:

```bash
npm run --prefix packages/webui screenshots:docs
```

Commit the image and its `.license` sidecar.
Use screenshots only when they clarify a visual workflow that prose and accessible labels cannot convey as efficiently.

## Validate the Site

```bash
uv run zensical build --clean --strict
```

Strict mode checks links, anchors, references, and footnotes.
Documentation contract tests also validate navigation, commands, release references, diagnostic routes, screenshot dimensions, and the absence of planning artifacts.
