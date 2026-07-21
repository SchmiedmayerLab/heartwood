<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood on Stanford Carina

Carina is a Stanford research-computing platform with project storage and Slurm-managed compute.
Heartwood uses a native release installation, the terminal interface, an optional Stanford AI API Gateway connection, and vLLM on requested GPU compute for Heartwood-managed models.

Do not access protected health information while evaluating Heartwood unless the complete deployment, model route, project, and task are approved for that data.

## Before You Begin

You need Carina access, the Stanford full-tunnel VPN, an authorized project directory, and enough project storage for the installation and any Heartwood-managed model.
Follow Carina's current [connection guide](https://docs.carina.stanford.edu/connect); login nodes are resource-limited and intended for setup, file management, and job submission rather than inference.

The guide below uses two sibling directories:

- `heartwood-installation/` for the release executable and Python/vLLM environments; and
- `heartwood-project/` for research files and `.heartwood/` state.

## Enter Approved Project Storage

Ask the project owner for the approved writable directory below `/projects`; its structure varies by allocation.
Enter that directory, confirm that it is writable, and create private installation and project directories:

```bash
cd /projects/APPROVED_PATH
test -w "$PWD" && echo "Project storage is writable"
df -h .
mkdir -m 700 heartwood-installation heartwood-project
```

Replace `APPROVED_PATH` with the path assigned to your group.
Do not use a shared project root itself as the Heartwood project.

## Install the Release

```bash
cd heartwood-installation
curl --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0-beta.4/heartwood-installer
chmod 700 heartwood-installer
./heartwood-installer --platform carina
export PATH="$PWD/bin:$PATH"
```

The version-stamped installer downloads the matching native archive and checksum, verifies them, checks storage, prevents concurrent updates to the same installation, loads the configured micromamba module when needed, and assembles a private source-and-runtime generation before making it current.
Dependency resolution and the vLLM environment can take several minutes; the installer reports seven named stages and elapsed time.

To keep the command available in a later shell, add the printed `bin` path through your normal shell configuration or export it again.

## Open the Project

```bash
cd ../heartwood-project
heartwood doctor
heartwood
```

Heartwood detects Carina, uses this exact directory as the project, and creates `.heartwood/` only after confirmation.
The supported Carina interaction surface is the terminal; the platform adapter does not advertise a Heartwood browser route.

## Choose a Model Route

### Stanford AI API Gateway

Choose **Research environment** when your project has an eligible Stanford AI API Gateway key and the route is approved for the intended data.
Heartwood queries the gateway for available models and keeps an entered key only for the running process unless the deployment supplies a mounted secret binding.

Stanford's [AI API Gateway service page](https://uit.stanford.edu/service/ai-api-gateway) describes current access, data-security status, rates, and support.
Those current Stanford terms, not Heartwood platform detection, determine data eligibility.

### Heartwood-Managed GPU Model

Choose **Run with Heartwood**, select a recommended model or another public Hugging Face repository, and review the download and resource plan.
Model files are stored under the project's `.heartwood/models/`, not the installation directory.

When you start Heartwood with a selected Heartwood-managed model, it discovers GPU-capable Slurm partitions, prints the complete `srun` request, and asks before allocating compute.
The current Carina partitions are `dev`, `normal`, and `long`; all can provide GPUs, with different time limits described in the official [Slurm guide](https://docs.carina.stanford.edu/slurm-carina).

For a short interactive session, the default `dev` partition is appropriate:

```bash
heartwood
```

If no default GPU partition can be selected, inspect the plan and specify one through the advanced runtime command:

```bash
heartwood runtime start --partition dev --time 01:00:00
```

Heartwood prepares the compatible runtime libraries, scopes model caches to the project, waits up to ten minutes by default, and reports the current stage and elapsed startup time every 15 seconds.

## Review, Exit, and Return

Use the terminal workflow normally.
Action sets are displayed together and resolved with `/allow` or `/reject`; internal confirmation identifiers do not need to be copied.

Exit with `/exit`.
The interactive Slurm allocation and supervised vLLM process end with the Heartwood process, while the project, model files, sessions, and audit records remain in project storage.

## Troubleshooting Carina

- If a command disappears or is killed on a login node, stop and use Slurm for the compute work; Carina documents strict login-node limits.
- If a partition is unavailable, run `sinfo --noheader --format='%P|%G|%a'` and choose one of the GPU-capable partitions Heartwood reports.
- If model startup fails, inspect `.heartwood/logs/` and the `HW-COMPUTE-*` checks from `heartwood doctor` without sharing project content or secrets.
- If an interactive allocation disconnects, the process ends with the terminal; Carina recommends `tmux`, `screen`, or a batch job for work that must survive a connection loss.
- Use Carina's [troubleshooting guide](https://docs.carina.stanford.edu/troubleshooting) for platform and Slurm failures.
