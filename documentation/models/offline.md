<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Move a Model Into an Offline Environment

Heartwood can package a selected model in a connected project and verify it into a different project that has no network access.
The portable bundle includes the immutable model source, license record, runtime settings, selected files, and a SHA-256 digest for every file.

The bundle does not authorize the transfer or approve the model for a dataset.
Use an institution-approved transfer channel and review the displayed license and deployment warnings before import.

## Before You Begin

You need:

- the same Heartwood release in the connected and offline environments;
- enough connected storage for the selected model and one additional uncompressed bundle;
- enough destination storage for the bundle, the imported model, and at least 512 MiB of import reserve;
- a model runtime compatible with the destination compute; and
- an approved way to move the bundle between environments.

Heartwood images and installers contain inference software but no model weights.
The bundle is a separate file and must remain outside source control and container images.

## 1. Prepare the Model While Connected

Enter the project that already has the intended model, or create a staging project and download one:

```bash
mkdir heartwood-model-staging
cd heartwood-model-staging
heartwood models download qwen25-7b-instruct-q4_k_m
heartwood doctor
```

`heartwood models managed` lists the recommendations available for the detected environment.
For another supported Hugging Face model, follow [Choose a Heartwood-Managed Model](choose-managed.md#other-hugging-face-models) before continuing.

## 2. Create the Verified Bundle

Create the destination directory first, then export the model selected in the current project:

```bash
mkdir -p /approved-transfer
heartwood models export /approved-transfer/heartwood-model.heartwood-model.zip
```

Heartwood verifies the source model before writing the bundle, hashes the bytes again while writing, and never replaces an existing output file.
Repeating an export from unchanged model files and metadata produces the same bundle bytes.

Large models can take several minutes to verify and copy.
The terminal and browser report byte progress and allow a transfer to be cancelled without publishing a partial bundle.

Move the completed `.heartwood-model.zip` file through the approved transfer process.
Do not include provider credentials or unrelated `.heartwood/` state.

## 3. Inspect the Bundle in the Destination

Enter the empty destination project and inspect the bundle before making changes:

```bash
mkdir offline-analysis
cd offline-analysis
heartwood models inspect-bundle /approved-transfer/heartwood-model.heartwood-model.zip
```

Review the model, immutable revision, license, runtime, file count, size, and platform warnings.
Checksums establish that the imported files match this bundle; they do not establish who supplied it.
Heartwood therefore does not accept bundle-supplied platform qualification for an unknown model.
An exact match to the installed Heartwood catalog uses the catalog's trusted qualification metadata; every other imported model remains **Not tested**.

## 4. Approve and Import

Import from the same project directory:

```bash
heartwood models import /approved-transfer/heartwood-model.heartwood-model.zip
```

Heartwood displays the review again and asks for explicit license approval.
For reviewed automation, `--approve-license` supplies that explicit decision without an interactive prompt.
The import is bound to the exact manifest shown during review and stops if the bundle changes before copying begins.

The import streams every file into private staging storage, checks its size and SHA-256 digest, verifies the runtime-specific model structure, and publishes it atomically under `.heartwood/models/`.
It also selects the imported model through the normal project configuration path; no manual state edit is needed.

## 5. Verify and Start

Confirm readiness and inspect the runtime plan without starting inference:

```bash
heartwood doctor
heartwood runtime start --dry-run
```

Then start Heartwood normally:

```bash
heartwood
```

The runtime planner uses the transferred model's verified files and compatibility settings in the same way as a connected download.
It does not try to retrieve transferred model files from Hugging Face after a restart.

## Use the Browser or Notebook

On a platform with a supported browser route, open **Settings → Models → Move a model between environments**.
Export and import use server-visible paths rather than uploading multi-gigabyte files through the page.
The browser shows the same plan, warnings, byte progress, cancellation state, and final project selection as the terminal.

The notebook bridge exposes the same gateway operations for programmatic workflows:

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

Use the terminal or browser for the license review unless a notebook workflow already has an equivalent human approval step.

## Run a Container Without Networking

After the bundle has been imported into the mounted project, a strict terminal-only container can start without a network namespace:

```bash
docker run --rm -it \
  --network none \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  -v "$PWD:/workspace" \
  ghcr.io/schmiedmayerlab/heartwood:0.3.0-beta.2 \
  heartwood
```

The mounted host directory remains the durable project and appears as `/workspace` in the container.
Docker's `none` network also isolates the container from the host, so this recipe does not provide a browser route.
An offline browser deployment requires an authenticated or loopback-only inbound route and a separately enforced outbound network boundary.

## Recover From an Interrupted Transfer

- **Export stopped:** rerun the export after confirming that the intended output path does not contain a completed bundle.
- **Import stopped:** rerun the same import; Heartwood removes private incomplete staging state before retrying.
- **Bundle changed after review:** inspect the transferred file again and investigate the transfer channel rather than bypassing the error.
- **Checksum or file-list mismatch:** obtain a new bundle from the connected project.
- **Destination already contains different files:** choose a clean project or remove the model only after confirming that no needed project uses it.
- **Runtime is unavailable or incompatible:** install the matching Heartwood runtime or choose a model supported by the destination; importing weights does not install GPU drivers or inference software.

Importing the same unchanged bundle again is safe: Heartwood fully verifies the existing project copy and returns it without duplicating model storage.

## Verify the Deployment Boundary

Before using project data, confirm that:

- the active model route is loopback;
- no hosted model credential is configured;
- the platform independently denies outbound network traffic;
- project and model storage meet the environment's handling requirements; and
- audit exports remain in approved storage.

Heartwood CI performs the connected export, clean no-network import, normal launcher plan, real model inference, OpenHands tool proposal, grouped approval and rejection, file verification, replay, and audit export as one acceptance path.
Deployment isolation and transfer authorization remain infrastructure responsibilities.
