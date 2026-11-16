<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Move a Model Into an Offline Environment

Heartwood can package the model selected in a connected project as a bundle and verify it into a project without network access.
The bundle holds the immutable model source, license record, runtime settings, selected files and a SHA-256 digest for every file.
It imports into a local launch's `.heartwood/models/`.

The bundle does not authorize the transfer or approve the model for a dataset.
Use an institution-approved transfer channel and review the displayed license and warnings before import.

## Prerequisites

You need:

- the same Heartwood release in the connected and offline environments;
- connected storage for the selected model plus one uncompressed bundle;
- destination storage for the bundle, the imported model and a 512 MiB import reserve;
- a model runtime that matches the destination compute; and
- an approved way to move the bundle between environments.

Keep the bundle out of source control and container images.

## 1. Prepare the Model While Connected

Enter the project that already uses the model, or create a staging project and download one:

```bash
mkdir heartwood-model-staging
cd heartwood-model-staging
heartwood models download qwen25-7b-instruct-q4_k_m
heartwood doctor
```

`heartwood models managed` lists the catalog for the detected environment.
For another Hugging Face model, follow [Other Hugging Face Models](choose-managed.md#other-hugging-face-models) first.

## 2. Create the Bundle

Create the destination directory, then export the model selected in the current project:

```bash
mkdir -p /approved-transfer
heartwood models export /approved-transfer/heartwood-model.heartwood-model.zip
```

Heartwood verifies the source model, hashes the bytes again while writing, and never replaces an existing file.
Exporting unchanged model files and metadata again produces the same bundle bytes.
The terminal and browser report byte progress and can cancel an export without publishing a partial bundle.

Move the completed `.heartwood-model.zip` file through the approved transfer process, without provider credentials or other `.heartwood/` content.

## 3. Inspect the Bundle in the Destination

Enter an empty destination project and inspect the bundle before changing anything:

```bash
mkdir offline-analysis
cd offline-analysis
heartwood models inspect-bundle /approved-transfer/heartwood-model.heartwood-model.zip
```

Review the model, immutable revision, license, runtime, file count, size and platform warnings.
Checksums establish that imported files match the bundle, not who supplied it.
Heartwood therefore ignores any qualification a bundle claims: a model that exactly matches the installed catalog uses the catalog's qualification, and every other imported model is **Not tested**.
For a vLLM directory the catalog pins the upstream revision but not the file contents, so import vLLM bundles only from a transfer path you control.

## 4. Approve and Import

```bash
heartwood models import /approved-transfer/heartwood-model.heartwood-model.zip
```

Heartwood shows the review again and asks for explicit license approval; `--approve-license` gives that decision for reviewed automation.
The import is bound to the manifest you reviewed and stops if the bundle changes before copying begins.
It streams each file into private staging, checks its size and digest, verifies the runtime-specific model structure, publishes the model atomically under `.heartwood/models/`, and selects it in the project configuration.
If it is cancelled or selection fails, fully verified files stay in the model cache and are verified again on retry; incomplete staging is removed.

## 5. Verify and Start

```bash
heartwood doctor
heartwood runtime start --dry-run
heartwood
```

The runtime planner uses the transferred files and settings exactly as for a connected download and never fetches them from Hugging Face after a restart.

## Use the Browser or Notebook

Where the browser interface is available, open **Settings → Models → Move a model between environments**.
Export and import use paths visible to the server rather than uploading files through the page, and show the same plan, warnings, progress, cancellation and final selection as the terminal.

The notebook bridge calls the same gateway operations:

```python
from pathlib import Path

from heartwood.notebook import NotebookSession

with NotebookSession() as session:
    plan = session.inspect_model_bundle(
        Path("/approved-transfer/heartwood-model.heartwood-model.zip")
    )
    transfer = session.import_model_bundle(
        Path(plan["bundle_path"]),
        approved=True,
        manifest_sha256=plan["manifest_sha256"],
    )
```

Use the terminal or browser for the license review unless the notebook workflow has an equivalent human approval step.

## Run a Container Without Networking

After the bundle is imported into the mounted project, a terminal-only container can run without a network namespace:

```bash
docker run --rm -it \
  --network none \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.5.0 \
  heartwood
```

Docker's `none` network removes external interfaces and keeps container-local loopback.
The bind mount still exposes the current host directory, so this is a network boundary, not host isolation, and there is no browser route.

## Recover From an Interrupted Transfer

- If an export stopped, confirm that the output path holds no completed bundle and export again.
- If an import stopped, rerun the same import; private incomplete staging is removed first.
- If the bundle changed after review, inspect it again and investigate the transfer channel rather than bypassing the error.
- If checksums or the file list do not match, obtain a new bundle from the connected project.
- If the destination already holds different files, use a clean project, or remove the model only after confirming that no project needs it.
- If the runtime is unavailable or incompatible, install the matching Heartwood runtime or choose a model the destination supports; importing weights installs no drivers or inference software.

Importing the same unchanged bundle again verifies the existing copy and returns it without duplicating storage.

## Verify the Offline Boundary

Before using project data, confirm that:

- the active model is Heartwood-managed and no hosted model credential is configured;
- the platform independently denies outbound network traffic;
- project and model storage meet the environment's handling requirements; and
- audit exports stay in approved storage.

CI runs the connected export, a no-network import into a read-only container, the runtime plan, real model inference, an OpenHands tool proposal, approval and rejection, file checks, replay and audit export as one acceptance path on Linux AMD64.
Transfer authorization and the environment's network isolation remain infrastructure responsibilities.
