<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Local and Offline Models

Heartwood can use a model without sending prompts to a hosted provider. The application and container images include local-inference software, but they intentionally contain no model weights. You choose and acquire model artifacts separately so their source, license, storage, and resource requirements remain explicit.

## Choose a Local Setup

Use one of these paths:

1. **Existing service:** connect to an Ollama, vLLM, SGLang, llama.cpp, or other OpenAI-compatible server that is already running.
2. **Reviewed download:** let Heartwood download, verify, select, and launch a reviewed model artifact in the current project.
3. **Platform-managed runtime:** on Carina, let `heartwood launch` request a GPU allocation and supervise the packaged vLLM runtime.

An existing service is the smallest download and storage commitment. A reviewed download provides the most reproducible demonstration. Neither choice is a model-quality, biomedical-validity, or institutional-approval claim.

## Use an Existing Service

Start the service according to its own documentation. The built-in Local connection expects an OpenAI-compatible server on loopback port `8765` with `/v1/models` and `/v1/chat/completions` routes.

```bash
heartwood models refresh local
heartwood models connect local <model-id>
heartwood
```

For another URL, use Custom API in the web setup or the CLI `--base-url` option. Heartwood reuses the OpenHands and LiteLLM provider path after selection; it does not proxy or reimplement the inference server.

## Download a Reviewed Model

Run these commands from the project that should own the model and its sessions:

```bash
heartwood models artifacts
heartwood models download qwen25-7b-instruct-q4_k_m
heartwood launch --dry-run
heartwood launch
```

The browser provides the same operation. Start `heartwood serve`, open **Settings**, and choose a model under **On this device**. The browser identifies whether the model runs on CPU or requires an NVIDIA GPU, displays transferred and expected bytes, and changes the project status only after verification succeeds.

The download is written under `.heartwood/models/`, and the model, runtime, and standard local profile are saved in `.heartwood/config.toml`. Heartwood pins the upstream revision, validates the expected content, and publishes the artifact or snapshot only after verification succeeds. `heartwood launch` reads the saved selection, so no model, runtime, or cache path is required on later commands.

The reviewed `qwen25-7b-instruct-q4_k_m` artifact is a quantized GGUF demonstration model for the packaged CPU llama.cpp runtime. `qwen25-coder-7b-instruct-q4_k_m` is available for coding-output experiments. The Carina workflow uses the reviewed `qwen25-7b-instruct-vllm` snapshot with the packaged GPU vLLM runtime. Check `heartwood models artifacts` for the recorded size and minimum resources before downloading.

On a generic machine or in the generic container, `heartwood launch` uses the verified project artifact directly. On Carina, it verifies the snapshot again, copies it to job-local scratch, starts vLLM on loopback, waits for the selected model to appear in the runtime catalog, and removes the staged copy when the session exits.

Use the web interface instead of the terminal while Heartwood supervises the same local runtime:

```bash
heartwood launch --web
```

The command keeps the model process alive until the web server stops. On a notebook platform, open the proxy for port `8767`.

## Know What to Expect

A reviewed download belongs to the current project. Heartwood places the verified files in `.heartwood/models/<model-id>/`, records the selection in `.heartwood/config.toml`, and reuses both on later launches. Stopping Heartwood removes the model server process, not the downloaded files.

During `heartwood launch`, Heartwood reports each stage as it:

1. verifies the saved artifact and selects its packaged runtime;
2. checks that the local server can start on the current machine;
3. starts the server on loopback and waits for the selected model to become ready;
4. connects the shared Heartwood model profile;
5. opens the terminal conversation, or the browser when `--web` is present.

Large models can take several minutes to load, and the first CPU response can take longer than a hosted response. Heartwood reports elapsed startup time every 15 seconds. Leave the command running while the terminal or browser is in use; press `Ctrl+C` to stop both the interface and its supervised model server.

Use these commands when the next step is unclear:

```bash
heartwood doctor
heartwood launch --dry-run
```

`compute-required` means the reviewed local artifact is ready but its server is not running. The browser reports this as **Model runtime needed**, keeps the conversation unavailable, and points to `heartwood launch --web`. `recovery-required` means a configuration, artifact, runtime, or platform check failed. Runtime startup details remain inside the project at `.heartwood/logs/local-model.log`.

## Run the Container

Use one persistent project mount for configuration, sessions, models, logs, and analysis files:

```bash
docker volume create heartwood-project

docker run --rm -it \
  -v heartwood-project:/workspace \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood models download qwen25-7b-instruct-q4_k_m

docker run --rm -it \
  --network none \
  -v heartwood-project:/workspace \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood launch
```

The first command needs network access to retrieve the pinned artifact. The second command can run with Docker networking disabled because inference and the agent-model connection remain on loopback. The agent can still read and modify files in the mounted project; network isolation does not remove the need to review actions. To use `launch --web`, publish port `8767` and enforce the deployment's reviewed egress policy instead of Docker's `none` network, which intentionally prevents host access to the web port.

For a host directory instead of a named volume, mount the current directory at `/workspace`. The directory must be writable by container user `10001`; see [Container Images](container-images.md) for deployment details.

## Prepare an Air-Gapped Deployment

An air-gapped run requires all inputs to be present before network access is removed:

- the exact Heartwood image or native release bundle;
- the reviewed model artifact and verification metadata;
- the project and any required data;
- the deployment policy and platform controls;
- any additional Skills that have already passed review.

Import the model through an authorized transfer process into the project's `.heartwood/models/<artifact-id>/` location. Preserve the exact `SHA256SUMS` and provenance files produced by the reviewed download. Run `heartwood doctor` and `heartwood launch --dry-run` before opening the session.

No-internet validation has two distinct purposes. The deterministic loopback model fixture exercises OpenHands orchestration, policy, grouped confirmation, Skills, audit, CLI, web, and notebook contracts on every pull request without claiming inference quality. A separately triggered capable-model acceptance uses a reviewed model, requires a real OpenHands tool call and successful terminal execution, checks the expected synthetic artifact, and runs with container networking disabled.

From a repository checkout, run the deterministic gate with:

```bash
docker compose -f images/generic/compose.yaml run --rm --build heartwood
```

The resource-intensive capable-model workflow is available as the `run_capable_model` option on the Container Smoke Test workflow. It is not a substitute for evaluating model quality on the intended research tasks.

## Confirm Actions and Audit

Local inference changes where model requests are processed; it does not change the agent's authority. Heartwood still defaults to OpenHands `AlwaysConfirm`, displays each pending action set, and records the allow or reject result. When platform policy permits **Auto-Approve Low Risk**, OpenHands may execute low-risk actions automatically while all other risk levels continue to require grouped review.

Export the scrubbed record after a synthetic validation:

```bash
heartwood audit export
```

The audit export excludes prompts, model responses, tool arguments, filesystem paths, row values, and credentials. Review it before moving it outside the deployment boundary.
