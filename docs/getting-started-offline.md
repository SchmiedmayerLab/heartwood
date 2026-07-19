<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Run a Model Locally

Local inference keeps model requests on the current machine or compute environment. Heartwood images contain inference software but no model weights.

## Understand the Lifecycle

A local setup has three parts:

1. **Model files** are downloaded into the project's `.heartwood/models/` directory.
2. **The inference server** loads those files and exposes a loopback model API.
3. **Heartwood** connects the coding agent to that API and supervises the server while the interface is open.

Downloading prepares the files; it does not leave a server running.

## Choose CPU or GPU

| Environment | Runtime | Practical use |
|---|---|---|
| Portable Heartwood container | llama.cpp on CPU | Compatible workstation path; 7B agent turns can be slow |
| NVIDIA Heartwood image | vLLM on NVIDIA GPU | Interactive local inference on a compatible AMD64 host |
| Terra portable image | llama.cpp on CPU | Portable fallback, not the recommended interactive local path |
| Terra NVIDIA image | vLLM on an attached NVIDIA GPU | Recommended Terra local-model path |
| Stanford Carina native installation | vLLM in a reviewed Slurm allocation | Carina local-model path |
| Generic native installation | External local service | The release installer does not bundle an inference server |

% TODO: This might be confusing; some of the portable images do contain them? And the term "portable image" sounds a bit strange ... we should make this more approachabe ...
The portable image does not use an attached GPU. Choose an explicit `-gpu-nvidia` image for NVIDIA inference.

## Review Available Models

List models compatible with the current runtime:

```bash
heartwood models local
```

% TODO: Do we list default models that we provide here? If we provide them, they MUST be usable for the main goals of the project. We might even want to avoid listing models at all and just use this command to list all configured models and rather link to the proposed page that helps people find the right local models. We should somehow dmonstrate that e.g. with Carina or a capable GPU on carina we can actually do local inference to a good quality. Use the latest and greates of local and smaller models that are out there ...
Heartwood shows the runtime, download size, model context capacity, and conservative storage and memory guidance. The listed models are suitable for synthetic demonstrations, not claims of biomedical quality or production readiness.

% TODO: This needs some more context before; this is a cool feature and the main extension point ....
To inspect another public Hugging Face repository without downloading it:

```bash
heartwood models inspect <owner/model>
```

Heartwood uses repository metadata to choose a supported single-file GGUF for llama.cpp or a compatible standard snapshot for vLLM. It rejects unsupported model families, custom model code, split or ambiguous artifacts, and formats the packaged runtime cannot load.

## Download and Verify

% TODO: This should use the generic huggingace way; we might want to avoid pre-shipping model configurations at all. Remove all relevant code here and do a good cleanup. We should rather have documentation guiding a user how to choose the best possible local model ...
Download a listed model or a successfully inspected repository:

```bash
heartwood models download qwen25-7b-instruct-q4_k_m
```

```bash
heartwood models download <owner/model>
```

% TODO: SOme of this here doesn't sound approachable.
Review `heartwood models local` or `heartwood models inspect <owner/model>` before downloading; those commands show the resolved source, expected size, free-space requirement, runtime choice, and approximate resource envelope. The direct download command starts the transfer immediately, reports progress, verifies the completed artifact, and records immutable provenance before selection.

The documented workflow uses public repositories. Access to private or gated snapshots varies by runtime and is not a portable Heartwood workflow.

## Start the Model

% TODO: As noted somehwere else, we should have a unified way to launch the elements. + doesn't the main heartwood command technically do all of that anyways? We should see that it is smart enough and offers intercative choices to easily naviage all of this; we should document it as well ...
Open the terminal with the model server running:

```bash
heartwood launch
```

Or start the model and browser together:

```bash
heartwood launch --web
```

The launcher verifies the artifact, checks available memory and compute, derives a resource-aware context-size estimate, starts the inference server, waits for readiness, and then opens the interface. If the estimated memory cannot support the minimum context, Heartwood warns before startup; the runtime may still fail, so reduce model or context requirements before using the project. Keep the process running. Exiting Heartwood stops the supervised server.

Model startup can take several minutes. Progress output includes the current stage, elapsed time, and log path. If startup fails, inspect `.heartwood/logs/local-model.log` and run `heartwood doctor`.

% TODO: This is a big jump; where does this come from? Docker setup either needs to be linked first or should rather be somewhere else?
## Use a Model Offline After Download

After a model has been downloaded and verified, the portable container can run a terminal session without network access:

```bash
docker run --rm -it \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --network none \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0-beta.3 \
  heartwood launch --plain
```

This starts the selected model and agent interface without a network after preparation. To verify the complete workflow, submit a bounded task that invokes the intended Skill and tool, review the action group, inspect the output, exit, and replay the session in a second offline container. Heartwood has no general command for importing externally transferred model files into a new air-gapped project.

Network isolation does not replace action review. The agent can still read and modify files available to the Heartwood process.

## Connect an Existing Local Service

% TODO: Provide some examples and context here ...
Heartwood can use an already running OpenAI-compatible service on loopback. Start `heartwood`, choose **On this device**, and select a model reported by that service. The process that owns the service remains responsible for model files, startup, shutdown, and resource limits.
