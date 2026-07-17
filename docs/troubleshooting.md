<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Troubleshoot Heartwood

Start here when setup, model access, or an interface does not behave as expected. The checks on this page are read-only unless a command explicitly says otherwise.

## Start with the Project Diagnostic

Run the diagnostic from the directory that Heartwood should treat as the project:

```bash
pwd
heartwood doctor
```

The first line confirms the project boundary. The diagnostic then checks project state, model selection, credential binding, route policy, and required compute.

| Readiness | Meaning | Normal next step |
|---|---|---|
| `ready` | The configured model and policy are available. | Run `heartwood`, `heartwood serve`, or a notebook task. |
| `setup-required` | The project does not yet have a complete model connection. | Run `heartwood` and complete guided setup. |
| `compute-required` | A downloaded local model is configured but its inference server is not running. | Run `heartwood launch` or `heartwood launch --web`. |
| `recovery-required` | One or more configuration, artifact, runtime, or platform checks failed. | Read the failed checks, correct them, and run `heartwood doctor` again. |

`compute-required` is healthy between local-model sessions. It does not mean that the model download failed.

## Resolve Project and Persistence Problems

??? question "Why does Heartwood look unconfigured in this directory?"

    Heartwood uses the current directory as the complete project boundary. Confirm that `pwd` identifies the directory containing the expected files and `.heartwood/`. Starting in a parent or nested directory intentionally opens a different project.

??? question "Why do the terminal, browser, and notebook show different sessions?"

    Start every interface from the same project directory and use the same session identifier. In a notebook, `Path.cwd()` must resolve to the same directory shown by `pwd` in the terminal. Wait until the active turn is idle before continuing that session from another process.

??? question "Why did project state disappear after replacing a container or cloud environment?"

    The complete project directory, including `.heartwood/`, must live on durable storage. Mount the project at `/workspace` in the generic container. On Terra, keep it under `/home/jupyter` and retain the persistent disk. Provider tokens are process-local and must be supplied again unless the deployment provides an approved secret binding.

See [Project Files and State](project-state.md) for the full storage contract.

## Resolve Model Setup Problems

??? question "Why does a downloaded model not answer requests?"

    Downloading model files does not start an inference server. Run `heartwood launch` for the terminal or `heartwood launch --web` for the browser, and keep that process running while using the model.

??? question "Why is a Hugging Face model rejected before download?"

    Heartwood accepts a repository only when it can choose one unambiguous artifact and a compatible packaged runtime. Inspect the plan first:

    ```bash
    heartwood models inspect <owner/model>
    ```

    Split or ambiguous GGUF files, unsupported model families, custom model code, incomplete metadata, and incompatible weight formats are rejected instead of guessed. Use a recommended model or follow the error's model-support issue link.

??? question "Why did a model download stop?"

    Confirm that the project storage has enough free space and that the repository is accessible. Private or gated Hugging Face repositories require their access terms and `hf auth login` before download. Rerun the same download command; Heartwood uses project-local temporary storage and verifies the completed artifact before activation.

??? question "Why does local model startup take several minutes?"

    Heartwood verifies the artifact, checks memory, starts the inference server, loads the model, and waits for a health response before opening the interface. The launcher reports elapsed time every 15 seconds. If the runtime exits before readiness, the launcher reports an early exit rather than waiting for the full timeout. Inspect `.heartwood/logs/local-model.log` and the diagnostic printed by the launcher.

??? question "Why is the GPU unused or out of memory?"

    The portable image always uses its CPU runtime. NVIDIA inference requires an explicit `-gpu-nvidia` image, a compatible NVIDIA driver, and enough accelerator memory for both the model and configured context window. Heartwood does not silently fall back to CPU after a GPU failure. Use a smaller recommended model or the portable image intentionally.

See [Run a Model Locally](getting-started-offline.md) for model formats, CPU and GPU paths, resource planning, and offline use.

## Resolve Connection Problems

??? question "Why can Heartwood list a provider but not use the selected model?"

    Model discovery and model completion are authorized separately. Run `heartwood doctor`, then refresh and validate the connection without printing credentials:

    ```bash
    heartwood models list
    heartwood models refresh <connection-id>
    heartwood models validate
    ```

    A token entered interactively remains only in the current Heartwood process. Supply it again after restart unless the deployment owns an approved persistent binding.

??? question "Why does a custom endpoint fail?"

    Remote custom endpoints must use HTTPS, while loopback HTTP is permitted for a service on the same machine. Confirm that the base URL exposes compatible model-list and chat-completion routes and that deployment policy authorizes the same origin.

See [Connect a Model](model-connections.md) for each supported connection type and its credential behavior.

## Resolve Interface Problems

??? question "Why does the browser page not open?"

    Confirm that `heartwood serve` or `heartwood launch --web` is still running. For local use, open `http://127.0.0.1:8767/`. A remote platform may require an authenticated proxy instead of a public port.

??? question "Why does the browser open but remain unavailable?"

    Open **Settings** and review **This project**, or run `heartwood doctor` in the same directory. A downloaded local model must be supervised by `heartwood launch --web`; standalone `heartwood serve` does not start it.

??? question "Why does a Terra browser link return 404?"

    Terra requires its complete authenticated Jupyter proxy URL. Generate the link with the Terra tutorial notebook while the Heartwood web process is running. Do not shorten it to a generic `/proxy/8767/` path. Continue in the terminal if the platform proxy is unavailable.

??? question "Why can a notebook replay a session but not submit a task?"

    The notebook bridge shares project state; it does not start a downloaded model. Configure the project first and keep `heartwood launch --web` running for a Heartwood-managed local model. Use one active writer for each session.

??? question "Why did opening Terra's terminal resume a paused environment?"

    The terminal route is an application route and can request running compute. Use the workspace dashboard's cloud controls or the Cloud Environments manager to inspect, pause, replace, or delete compute without first opening the terminal. Keep the persistent disk when the project must survive replacement.

See [Browser and Notebooks](web-interface.md) for interface setup and [Use Heartwood on Terra](terra-jupyter-demo.md#troubleshoot-the-terra-workflow) for platform-specific checks.

## Resolve Action Review Problems

??? question "Why can I approve only the complete action set?"

    OpenHands exposes one confirmation stop for the displayed action set. Heartwood therefore presents **Allow all once** and **Reject all** for the group instead of implying that individual members can execute independently. Reject the group when any member is unnecessary, unclear, or out of scope.

??? question "Why is an action waiting?"

    The agent pauses at a confirmation stop until the interface that owns the active turn allows or rejects it. Do not try to approve the same turn simultaneously from another terminal, browser, or notebook process.

See [Work with the Agent](using-heartwood.md#review-the-complete-action-set) for the normal approval workflow.

## Collect Safe Diagnostic Information

Before opening an issue, collect only information that is safe to share:

- Heartwood version and immutable container or release identifier;
- the output of `heartwood doctor`, after checking that platform-specific details are appropriate to disclose;
- the failing command and its exit status;
- the relevant runtime error without prompts, tokens, participant-level data, or unrelated file paths;
- whether the project uses a container, Terra, Carina, or another environment;
- whether the model is hosted, already running locally, or Heartwood-managed.

Do not attach `.heartwood/`, provider credentials, full conversation logs, environment dumps, protected data, or broad directory listings. Use the [GitHub issue chooser](https://github.com/SchmiedmayerLab/heartwood/issues/new/choose) after removing sensitive content.

## Continue

- Return to [Get Started](getting-started.md) after the diagnostic reports `ready`.
- Use [Platform Support and Validation](platform-support.md) to distinguish a software problem from a platform or approval boundary.
- Operators should continue with [Deploy Heartwood](deployment.md) when the issue involves persistent storage, secrets, routing, or isolation.
