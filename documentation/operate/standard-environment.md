<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Define a Standard Research Environment

A standard research environment gives a group of researchers the same Heartwood release, analysis software, storage layout, and update path.
Define it once, record it, and verify every researcher's environment against that record before project data is added.

This page uses Terra as the worked example.
The same record applies to Carina, containers, and other platforms, each with its own storage and access controls.

## Record the Definition

Keep one environment record for each supported release.
Store it with the deployment's operating documentation rather than inside a research project.

| Element | What to Record | Terra Example |
|---|---|---|
| Heartwood release | Release tag and verified image digest | `ghcr.io/schmiedmayerlab/heartwood:0.4.0-terra` and its `sha256` digest |
| Platform base | Base image family and version | `terra-jupyter-python` 1.1.6 |
| Compute | CPU, memory, disk, and GPU shape | 8 CPUs, 30 GB memory, and a 50 GB persistent disk for a hosted model route |
| Model routes | Routes approved for the intended data and the default for new projects | Stanford AI API Gateway, or a qualified model from the [GPU compatibility matrix](../reference/gpu-compatibility.md) |
| Action review | The mode researchers use | **Review Every Action** |
| Audit evidence | Signer profile and where checkpoints are retained | The deployment signer and a retained location outside the project |
| Analysis dependencies | How each project records its Python environment | A `pylock.toml` dependency lock and the record from `heartwood experiments environment --python` |
| Support | Owner, contact, and update review date | A named maintainer and a quarterly review |

Use [Configure a Reviewed Assistant](reviewed-assistant.md) to choose the model-route, action-review, and audit entries.

## Understand the Image

Heartwood platform images add one application payload to the platform's own base image.

- The platform base keeps its user, home directory, Jupyter runtime, entry point, and proxy behavior.
- Heartwood installs in `/opt/heartwood` with its own locked Python environment, separate from the analysis environment.
- The Terra image adds the **Python 3 (Heartwood)** notebook kernel for the [notebook bridge](../use/notebooks.md).
- The GPU variant adds the locked inference runtime listed in the GPU compatibility matrix.
- No image contains model weights or credentials.

Release images are built from digest-pinned base images and locked dependencies.
Terra tags use a single-platform manifest because Terra's image detection requires it, and they carry no registry attestations.
Record and deploy the digest you verified rather than relying on the tag alone.

## Lay Out Project Storage

Give each project its own directory on durable storage and start Heartwood from that directory.
On Terra, use a directory below `/home/jupyter` on the persistent disk.

```text
project/
├── data/         input files, kept unchanged
├── results/      workflow and analysis outputs
└── .heartwood/   private configuration, sessions, and audit state
```

Keep original data unchanged and write each workflow run to an unused output folder.
Copy durable results and retained evidence to workspace storage.
Do not copy `.heartwood/` into shared or public locations; see [Projects and Private State](../start/project.md).

Google Cloud encrypts Terra persistent disks and workspace buckets at rest by default.
Confirm whether the workspace needs additional controls, such as customer-managed encryption keys or an Authorization Domain, before adding controlled data.

## Control Access

Terra authenticates each researcher and gives each researcher a separate Cloud Environment in a workspace.
Workspace sharing and Authorization Domains decide who can read the workspace bucket and its data.
Heartwood does not add its own user directory; it runs as the researcher inside that boundary.

## Verify Each Environment

Before a researcher adds project data:

1. Run `heartwood --version` and compare it with the recorded release.
2. Run `heartwood doctor` from the project directory and resolve every failed check.
3. Complete one [research workflow](../use/research-workflows.md) on synthetic inputs and export its record with `heartwood experiments export`.
4. Recreate the compute while retaining the persistent disk, then confirm that the project, its sessions, and its experiment records remain.

The environment matches the record when the version agrees, `heartwood doctor` reports no failed check, and the synthetic workflow completes after compute recreation.

## Update the Environment

Review the latest stable release at least once per quarter and apply a published security patch promptly, as described in [Support and Compatibility](support.md#update-cadence).

1. Read the release notes and back up `.heartwood/` if you may need to roll back.
2. On Terra, delete only the Cloud Environment, retain the persistent disk, and create it again with the new tag.
3. Repeat the verification steps and record the new digest.

Project state migrates forward only.
An older release does not open state that a newer release has migrated, so a rollback restores the backup taken before the update.
