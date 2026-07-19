<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Supported Environments

% TODO: Make this more generic than one release; it should actually be a bit more extensive as it seems like this might be more technical in nature. Ensure that it doesn't duplicate information from other pages; make sure that its going a bit more detailed and maybe be a platform guide on how to add additional environments? Maybe also adjust the title and positionign accordingly ...
Release `0.2.0-beta.3` is pre-1.0 software. The table describes available artifacts and documented interfaces, not institutional authorization for controlled data.

| Environment | Artifact | Architecture | Interfaces | Local inference |
|---|---|---|---|---|
| Docker workstation or Linux host | Generic container | AMD64, ARM64 | Terminal, browser, notebook bridge | Packaged CPU llama.cpp |
| NVIDIA Docker host | Generic GPU container | AMD64 | Terminal, browser, notebook bridge | Packaged NVIDIA vLLM |
| Terra Jupyter | Terra-derived container | AMD64 | Terminal, authenticated browser proxy, notebook | CPU llama.cpp |
| Terra Jupyter with GPU | Terra-derived GPU container | AMD64 | Terminal, authenticated browser proxy, notebook | NVIDIA vLLM |
| Stanford Carina | Native installer | AMD64 Linux | Terminal | NVIDIA vLLM through Slurm |
| Generic native Linux | Native installer | Platform-dependent | Terminal; Python notebook API when operator-integrated | External service required |

## Current Boundaries

- Published images contain no model weights or provider credentials.
- Where packaged, the terminal and browser provide the complete researcher conversation and setup workflow. The notebook bridge does not supervise local models or manage Skills.
- Stanford Carina has no documented authenticated Heartwood browser route.
- Terra browser access depends on the complete authenticated Jupyter proxy path.
- One process may write a session at a time; concurrent independent writers are unsupported.
- Bundled data detection and biomedical workflows use synthetic fixtures. Heartwood does not identify or authorize a real biomedical dataset.
- Recommended local models are demonstration choices, not biomedical, production, benchmark, license, or institutional approval.
- Another managed research platform requires its own reviewed image, storage, identity, proxy, and policy integration.

## Evidence and Approval

Automated checks protect the documented project, model, session, interface, container, Terra, and Carina contracts with synthetic data. They do not reproduce an institution's identity, data, network, or approval controls.

Real-platform checks are useful deployment evidence but do not establish a business associate agreement, dataset authorization, clinical validity, or institutional approval. The deploying institution must review the exact artifact, model route, credentials, data use, network controls, and operational ownership.

Follow the [Terra](terra-jupyter-demo.md), [Carina](carina-cli.md), or [container](container-images.md) guide for the supported user path.
