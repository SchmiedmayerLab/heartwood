<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

% TODO: Not sure if this makes sense here, isn't that only applicable to a subset of elements in the documentation. And generally figure out what do do here ... we might also be able to merge that into other files that already touch on this and therefore might resuce the overall number of files?! It seems a bit redundant and mighe be part of some other elements to be conslidated?

# Choose an Environment

Heartwood should run in the approved computing environment that already contains the files it may use. The environment determines the installation, persistent storage, model routes, and available interfaces.

## Compare the Options

| Situation | Use | Why |
|---|---|---|
| Learning on a workstation | Generic container | Shortest route to the terminal and browser without installing the Python stack |
| General Linux server | Generic container or native terminal installation | Choose according to local container and software policy |
| Terra workspace | Terra-derived image | Preserves Terra Jupyter, persistent disk, and authenticated routing |
| Stanford Carina project | Native installation | Integrates with shared project storage and Slurm GPU allocation |
| Another managed platform | Operator-reviewed deployment | Platform identity, storage, proxy, and image requirements need explicit validation |

The portable images include CPU inference software but no model weights. Explicit NVIDIA images add GPU inference software. A hosted or research-environment model does not require either local inference path.

## Keep the Project on Durable Storage

The current directory is always the project:

- Mount the host project at `/workspace` in the generic container.
- Create the Terra project below `/home/jupyter` on the retained persistent disk.
- Enter the approved Carina project directory before starting Heartwood.
- Enter the analysis directory before using a native installation.

Heartwood stores its private state inside the project. Preserving the whole directory preserves its configuration and sessions. [Projects and Persistent State](project-state.md) provides the details.

## Check What the Platform Owns

Heartwood can detect Terra and Carina and apply platform-specific defaults. It does not decide whether the environment, model provider, or dataset is institutionally approved.

Before controlled-data use, the deployment owner must verify:

- user identity and project access;
- persistent storage and backup;
- network and egress controls;
- model-provider agreements and service settings;
- dataset permissions and export rules; and
- the exact Heartwood artifact and platform integration.

## Follow the Matching Guide

- [Workstations and Linux servers](installation.md)
- [Containers](container-images.md)
- [Terra](terra-jupyter-demo.md)
- [Stanford Carina](carina-cli.md)
- [Institutional deployment](deployment.md)

[Supported Environments](platform-support.md) summarizes the current artifact and interface boundaries without treating technical availability as institutional approval.
