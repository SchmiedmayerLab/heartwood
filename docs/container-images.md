<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Run Heartwood in a Container

The generic Heartwood image is the easiest way to use the complete CLI and browser interface without installing Python, Node.js, OpenHands, or a local inference server on the host. It contains the Heartwood application, web assets, repository-verified Skills, OpenHands SDK, and CPU llama.cpp runtime. It contains no model weights and no credentials.

Use the Terra-derived image when Terra must retain ownership of Jupyter, the notebook route, user identity, and persistent disk. Use the native installer on environments such as Carina where the scheduler and shared filesystem are more important than container portability.

## Choose an Image

| Image | Platforms | Purpose |
|---|---|---|
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0` | AMD64 and ARM64 | Stable generic runtime and the normal container starting point. |
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-gpu-nvidia` | AMD64 | Generic runtime with an isolated vLLM environment for compatible NVIDIA deployments. |
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-terra` | AMD64 | Terra Jupyter base with the Heartwood payload added. |
| `ghcr.io/schmiedmayerlab/heartwood:0.2.0-terra-gpu-nvidia` | AMD64 | Terra-derived image with the isolated vLLM environment. |
| `edge` and `edge-*` | Flavor-specific | Latest validated `main` build for development, not a stable release. |
| `sha-<git-sha>` and `sha-<git-sha>-*` | Flavor-specific | Immutable images for one repository commit. |

The portable generic image remains the default. A GPU image still requires compatible host drivers, a suitable model, enough accelerator memory, and deployment-specific validation.

## Start a Project

Create or enter the directory the agent may edit, then mount that directory at `/workspace`:

```bash
mkdir heartwood-demo
cd heartwood-demo

docker run --rm -it \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood serve --host 0.0.0.0
```

Open `http://127.0.0.1:8767/`. Heartwood treats `/workspace` as the project and creates `/workspace/.heartwood/`. One project mount therefore preserves source files, configuration, sessions, downloaded models, Skills, logs, and audit records across replacement containers.

`/workspace` is the image's default mount target and working directory, not a separate Heartwood workspace setting. Mounting another directory and selecting it with Docker's `--workdir` changes the project in exactly the same way as changing directories before running the native CLI. Platform images such as Terra use their platform's persistent home directory instead of imposing this generic mount convention.

The image runs as non-root user `10001:10001`. On Linux, make the mounted project writable by that identity or run the container with a reviewed user mapping that can write the project. Do not make the application root writable merely to avoid a host-permission problem.

The same project can use the terminal interface instead:

```bash
docker run --rm -it \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood
```

## Use a Hosted or Existing Model Service

Start `heartwood serve`, open **Settings**, and select a platform connection, OpenAI, Anthropic, or Custom API. A token entered in the browser remains only in the gateway process. Heartwood stores the selected model and non-secret binding in the project, not the token.

The deployment must allow the selected catalog and completion routes. A container connection does not establish that a provider is suitable for protected data; the exact agreement, covered service, identity, retention, region, and network controls remain deployment decisions.

For production automation, provide credentials through a platform secret facility, mounted credential file, or managed identity and configure the corresponding non-secret binding. Do not bake credentials into a Dockerfile, image layer, label, build argument, or project file.

## Download and Run a Local Model

List the current recommendations, then download one into the project's `.heartwood/models/` directory:

```bash
docker run --rm -it \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood models local

docker run --rm -it \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood models download qwen25-7b-instruct-q4_k_m
```

You can instead inspect and prepare another Hugging Face model. The image chooses CPU llama.cpp for a supported single-file GGUF repository; the NVIDIA image chooses vLLM for a supported standard snapshot:

```bash
docker run --rm -it \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood models inspect <owner/model>

docker run --rm -it \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood models download <owner/model>
```

The download requires network access and sufficient project storage. Heartwood reports the automatic runtime and resource plan, shows transfer progress, verifies the immutable source and content, and records the selection without copying the model into an image layer. Unsupported or ambiguous repositories fail before transfer and link to the issue chooser.

Start the model and browser together:

```bash
docker run --rm -it \
  -p 127.0.0.1:8767:8767 \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood launch --web --host 0.0.0.0
```

For a no-network terminal demonstration after the artifact is present:

```bash
docker run --rm -it \
  --network none \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.2.0 \
  heartwood launch --plain
```

The portable image runs llama.cpp on CPU. Attaching a GPU does not accelerate that path. To use the explicit AMD64 NVIDIA variant, inspect or download the recommended `qwen25-7b-instruct-vllm` snapshot with that image, retain the same project mount, and start the container with GPU access such as Docker's `--gpus all`. The image supplies vLLM but still downloads model weights only after that explicit project-level command.

## Runtime Security Controls

The image supports a read-only application filesystem, dropped Linux capabilities, `no-new-privileges`, a bounded process limit, and writable project and temporary mounts. The exact controls depend on the deployment because Heartwood must still reach an authorized hosted model or expose the browser port when those features are selected.

Provider credentials are supplied directly to the in-process OpenHands model client after route authorization. Heartwood removes configured provider-key values from terminal subprocess environments. This is not a hard same-user process boundary; use an OpenHands remote workspace or platform-native isolation when tools must be unable to access the model identity.

## Terra Images

The Terra image starts from the pinned Terra Jupyter Python base and preserves its `jupyter` user, home directory, notebook server, kernel setup, entrypoint, port `8000`, and Leonardo route behavior. Heartwood is installed under `/opt/heartwood`; a separate `Python 3 (Heartwood)` kernel is registered without replacing Terra's environment.

Terra tags are intentionally AMD64 Docker schema-2 manifests. Leonardo image auto-detection rejects the multi-platform Open Container Initiative index used by the generic tag. Follow [Heartwood on Terra](terra-jupyter-demo.md) for project storage, notebook proxy, and synthetic validation.

## Publication and Validation

Pull requests build and validate the generic AMD64 and ARM64 images, Terra-compatible image, GPU dependency stages, no-weight contract, real OpenHands loopback flow, grouped action confirmation, local llama.cpp inference, project persistence, Jupyter routes, and notebook proxy behavior with synthetic data.

Publication builds candidates by digest, tests the exact staged descriptors, creates immutable commit tags, and moves `edge` only after validation. Release promotion copies those verified descriptors to the Semantic Version tags. Generic architecture manifests include supply-chain attestations. Terra disables index-producing attestations to preserve Leonardo's required single-manifest format.

See [Platform Images](platform-images.md) for the extension contract and [Platform Support](platform-support.md) for the distinction between implementation, continuous-integration validation, live validation, and institutional approval.
