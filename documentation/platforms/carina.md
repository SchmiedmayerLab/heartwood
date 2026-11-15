<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Use Heartwood on Stanford Carina

Carina is a Stanford research-computing platform with project storage and Slurm-managed compute.
Heartwood runs there from a native installation in project storage, with the terminal interface, an optional Stanford AI API Gateway connection, and vLLM on requested GPU compute for Heartwood-managed models.

Do not access protected health information while evaluating Heartwood unless the complete deployment, model route, project and task are approved for that data.

## Qualification

The Carina installer and its installation layout are tested in CI with a simulated Slurm environment; the Carina adapter reports validation level `ci`.
The two-L40S Muse Glimmer 30B BF16 configuration completed Heartwood's coding-agent qualification on Carina on 2026-08-14 with the pinned vLLM runtime listed in [GPU Compatibility](../reference/gpu-compatibility.md).
A Carina launch is a local launch: `.heartwood/` state lives in the project directory and agent tools run with your account's permissions.
The isolated worker deployment is not qualified on Carina.

## Before You Begin

You need Carina access, the Stanford full-tunnel VPN, an authorized project directory, and enough project storage for the installation and any Heartwood-managed model.
Follow Carina's [connection guide](https://docs.carina.stanford.edu/connect).
Login nodes are resource-limited and meant for setup, file management and job submission, not inference.

The guide uses two sibling directories: `heartwood-installation/` for the release and its Python and vLLM environments, and `heartwood-project/` for research files and `.heartwood/` state.

## Enter Approved Project Storage

Ask the project owner for the approved writable directory below `/projects`; its structure varies by allocation.

```bash
cd /projects/APPROVED_PATH
test -w "$PWD" && echo "Project storage is writable"
df -h .
mkdir -m 700 heartwood-installation heartwood-project
```

Replace `APPROVED_PATH` with your group's path, and do not use a shared project root itself as the Heartwood project.

## Install the Release

```bash
cd heartwood-installation
curl --proto '=https' --tlsv1.2 --fail --location --remote-name \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.4.0/heartwood-installer
chmod 700 heartwood-installer
./heartwood-installer --platform carina
export PATH="$PWD/bin:$PATH"
```

Where the GitHub CLI is available, `gh attestation verify heartwood-installer --repo SchmiedmayerLab/heartwood` confirms that the release workflow built the installer.
The installer downloads the matching native archive, the GPU runtime and their checksums, verifies them, checks storage, and assembles a private installation before making it current.
The GPU runtime is mirrored as a release asset because Carina blocks the upstream vLLM wheel host; its digest is pinned to the upstream artifact.
On a login node, the installer moves dependency installation into a CPU-only Slurm allocation on the `dev` partition; no GPU is requested.
It reports the allocation, seven named stages and elapsed time, and can take several minutes.

Add the printed `bin` path to your shell configuration, or export it again in each new shell.

## Open the Project

```bash
cd ../heartwood-project
heartwood doctor
heartwood
```

Heartwood detects Carina, uses this directory as the project, and creates `.heartwood/` only after you confirm.
The first command after installation can take tens of seconds while Python dependencies load; later commands start faster.
Carina offers the terminal interface only.

## Choose a Model Route

Carina's policy permits two routes: **Stanford AI API Gateway** and **Run with Heartwood**.

### Stanford AI API Gateway

Choose **Stanford AI API Gateway** when your project has an eligible key and the route is approved for the intended data.
Heartwood lists the models the gateway enables for that key and keeps an entered key only for the running process unless the deployment supplies a mounted secret.
Stanford's [AI API Gateway service page](https://uit.stanford.edu/service/ai-api-gateway) states the current access, data-security status and rates; those terms, not Heartwood's platform detection, decide data eligibility.

### Heartwood-Managed GPU Model

Start managed-model setup from the project directory:

```bash
heartwood setup --model-source heartwood
```

On a login node, the installed command moves download and verification into a CPU Slurm allocation with 8 CPUs, 64 GiB RAM and a two-hour limit on `dev`; set `HEARTWOOD_MODEL_PREPARATION_PARTITION` to use another CPU partition.
Select a catalog model or another public Hugging Face repository and review the download and resource plan.
Model files are stored in `.heartwood/models/` in the project, not in the installation, and every file is verified before the selection is saved; on project storage, verifying a large existing cache can take several minutes.
An approved `HF_TOKEN` can raise Hugging Face rate limits during the first download and is used only by the download.

| Tier | Model Configuration | GPUs | Recommended RAM | Free Project Storage | Default Context | Estimated Runtime Startup |
|---|---|---:|---:|---:|---:|---:|
| Maximum capability, qualified | Muse Glimmer 30B BF16 | 2 x L40S | 128 GB | 96 GB | 32,768 | 5-20 minutes |

The model download is about 55.5 GiB, and the startup estimate applies once it is in `.heartwood/models/`.
Complex agent turns can take several minutes after startup.
See [Choose a Heartwood-Managed Model](../models/choose-managed.md) for the catalog.

### Start GPU Compute

When you run `heartwood` with a managed model selected, Heartwood inspects the GPU-capable Slurm partitions, available L40S GPUs, memory limits and model cache.
It prints the strongest compatible qualified configuration, the expected startup range and the complete `srun` request, and asks before downloading weights and again before allocating GPUs.
Without a partition, Heartwood uses Slurm's default compatible GPU partition, usually `dev` on Carina.

If no default GPU partition can be selected, name one:

```bash
heartwood runtime start --partition dev --time 01:00:00
```

Preview a capability tier without downloading or allocating:

```bash
heartwood runtime start --task-profile maximum --dry-run
```

The preview neither selects nor downloads a model; complete setup before running `heartwood runtime start` without `--dry-run`.
`--task-profile` accepts `auto`, `standard`, `powerful` or `maximum`, and `--gpus` must match a GPU count at which a catalog configuration was qualified.
For scripted deployment, `--yes-download` and `--yes-request-allocation` are separate approvals; interactive use should keep both prompts.

Heartwood waits up to ten minutes for the runtime (`--startup-timeout`, in seconds) and reports the elapsed time every 15 seconds.
For multi-GPU allocations, Heartwood's vLLM launcher applies the NCCL communication fallback itself; do not set NCCL or custom all-reduce options manually.
The Carina 2.0 nodes, partitions and their time limits are described in Carina's [Slurm guide](https://docs.carina.stanford.edu/slurm-carina) and [Carina Facts](https://docs.carina.stanford.edu/carina-facts).

## Review, Exit, and Return

Use the [terminal workflow](../use/terminal.md); action sets wait for **Allow Once** or **Reject**, or `/allow` and `/reject` in the plain terminal.
Exit with `/exit`.
The interactive Slurm allocation and the supervised vLLM process end with Heartwood; the project, model files, sessions and audit records remain in project storage.

## Troubleshooting Carina

- If a command disappears or is killed on a login node, stop and use Slurm for the compute work; Carina enforces strict login-node limits.
- If the installer's `dev` partition is unavailable, inspect `sinfo --noheader --format='%P|%G|%a'` and retry with another CPU partition, for example `HEARTWOOD_INSTALL_PARTITION=normal ./heartwood-installer --platform carina`.
- If a GPU partition is unavailable, run the same `sinfo` command and choose one of the GPU-capable partitions Heartwood reports.
- If Slurm reports `QOSMaxGRESPerUser` or `QOSMaxMemoryPerUser`, the account cannot request the planned GPUs or RAM; ask the Carina project owner to review the account limits.
- If the model does not fit the available GPUs or memory, do not change tensor parallelism or precision manually.
- If startup reports a driver or CUDA incompatibility, keep the released CUDA 12.9 environment and report the detected driver; do not install CUDA 13 into the Heartwood runtime.
- If a Hugging Face transfer is interrupted, rerun the same command from the project directory; completed files are reused.
- If model startup fails, inspect `.heartwood/logs/` and the `HW-COMPUTE-*` checks from `heartwood doctor` without sharing project content or secrets.
- If an interactive allocation disconnects, the process ends with the terminal; use `tmux`, `screen` or a batch job for work that must survive a connection loss.
- For platform and Slurm failures, use Carina's [troubleshooting guide](https://docs.carina.stanford.edu/troubleshooting).
