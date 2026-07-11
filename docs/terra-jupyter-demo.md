<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Terra Jupyter Demo

This runbook exercises the published Heartwood image in a synthetic Terra workspace. It validates Jupyter startup, the Leonardo proxy, the Heartwood kernel, the CLI, the web conversation, model configuration, OpenHands action confirmation, replay, and audit export. Do not use controlled data until the deployment policy and institutional review are complete.

The Terra image and its continuous-integration contracts are implemented. The live workspace evidence in this runbook remains required before the repository can mark Terra live-validated. See [Platform Support](platform-support.md) for the current status and [02 — Platforms](../design/02-platforms.md) for rationale.

## Select The Image

Use:

```text
ghcr.io/schmiedmayerlab/heartwood:edge-terra
```

For reproducibility, replace `edge-terra` with `sha-<git-sha>-terra` after selecting a tested build. Main publishes both tags automatically from the pinned Terra Jupyter Notebook base image.

The Terra tag is a public, unauthenticated, `linux/amd64` Docker schema-2 image manifest with media type `application/vnd.docker.distribution.manifest.v2+json`. This is deliberate: Leonardo rejects an Open Container Initiative index during image auto-detection. `docker manifest inspect` alone is insufficient because it does not prove that the registry returns Leonardo’s accepted media type. Verify a published commit with:

```bash
python3 images/platform/scripts/verify_registry_manifest.py \
  --manifest images/platforms.toml \
  --platform terra \
  --image-name ghcr.io/schmiedmayerlab/heartwood \
  --git-sha <git-sha>
```

## Create A Synthetic Workspace

1. Create a Terra workspace that contains no protected health information.
2. Select the Heartwood custom image.
3. Use the default Jupyter service route and confirm that the notebook file browser loads instead of returning a 404.
4. Confirm that the `Python 3 (Heartwood)` kernel is available.
5. Record the custom image digest, Terra base digest, machine shape, persistent disk size, and startup time.

The image preserves Terra’s `jupyter` user, `/home/jupyter` home, `/opt/conda` platform Python, Jupyter entrypoint, `/etc/jupyter/scripts/run-jupyter.sh`, and `/notebooks/...` route. Heartwood is installed separately under `/opt/heartwood`.

The current image pins `terra-jupyter-python:1.1.6`, which remains listed in Terra's official image catalog. Terra also publishes the newer slim `terra-base:1.0.0`; Heartwood does not switch bases until the complete runtime contract in this runbook passes against the replacement.

## Prepare The Reference Workspace

Terra mounts a Jupyter persistent disk at `/home/jupyter`. Heartwood keeps session events, OpenHands workspaces, downloaded model artifacts, and generated analysis files under that mount so they survive an application restart when the disk is retained. The workspace bucket is separate storage; collaborators cannot see files left only on a user's persistent disk.

Create one named synthetic analysis workspace from the checked-in image fixture:

```bash
export HEARTWOOD_STATE_ROOT=/home/jupyter/heartwood-workspace
export HEARTWOOD_WORKSPACE="${HEARTWOOD_STATE_ROOT}/sessions"
export HEARTWOOD_SESSION_ID=terra-demo

mkdir -p "${HEARTWOOD_STATE_ROOT}/workspaces/${HEARTWOOD_SESSION_ID}/input"
cp /opt/heartwood/fixtures/synthetic/omop-like/*.csv \
  "${HEARTWOOD_STATE_ROOT}/workspaces/${HEARTWOOD_SESSION_ID}/input/"

heartwood --workspace "${HEARTWOOD_WORKSPACE}" \
  --session-id "${HEARTWOOD_SESSION_ID}" detect
```

The fixture contains 24 synthetic people, 39 condition-occurrence rows, 20 people with condition concept `201826`, and no protected health information. Every person has recorded condition history, and the positive and negative groups overlap in age. Its size and class balance are deliberate: the reference cohort passes a count floor of 20, the low-count tests exercise suppression separately, and the baseline has both outcome classes without a perfectly separated age feature.

For institution-approved workspace data, localize only the required files from the workspace bucket into the named analysis workspace. Terra exposes `WORKSPACE_NAMESPACE`, `WORKSPACE_NAME`, and `WORKSPACE_BUCKET`; for example:

```bash
gcloud storage cp \
  "${WORKSPACE_BUCKET}/approved-reference-input/person.csv" \
  "${HEARTWOOD_STATE_ROOT}/workspaces/${HEARTWOOD_SESSION_ID}/input/person.csv"
gcloud storage cp \
  "${WORKSPACE_BUCKET}/approved-reference-input/condition_occurrence.csv" \
  "${HEARTWOOD_STATE_ROOT}/workspaces/${HEARTWOOD_SESSION_ID}/input/condition_occurrence.csv"
```

Terra data tables hold metadata and file references; they do not make files available inside the container automatically. The current release has no live Terra or BigQuery OMOP data-source adapter, so the commands above describe the platform localization pattern rather than a supported controlled-data workflow. Keep this validation synthetic until the adapter, deployment policy, data permissions, and institutional review gates in the roadmap are complete.

## Configure A Model

The image contains no model weights. Choose one path.

For an institution-authorized endpoint, the deployment supplies a platform connection manifest or enables a built-in connection. Its credential remains in the workspace environment, mounted secret file, or managed identity. List the available connections and select an exact identifier returned by the authorized catalog:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models list
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models refresh <connection-id>
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models connect \
  <connection-id> <model-id>
```

The workspace must supply `HEARTWOOD_POLICY_PROFILE` with the exact catalog endpoint, completion endpoint, capability tier, and non-secret credential reference such as the environment-variable name. `HEARTWOOD_MODEL_CONNECTIONS` may point to a platform-owned manifest that exposes every model available to the workspace identity. See [Model Connections](model-connections.md). A provider name does not establish HIPAA eligibility or a business associate agreement.

For a local synthetic demo, list and download a reviewed artifact to Terra-persistent storage:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models artifacts
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models download \
  qwen25-7b-instruct-q4_k_m \
  --cache /home/jupyter/heartwood-workspace/models
```

Start the included CPU server in a terminal using the exact path printed by the download:

```bash
cd /opt/heartwood
HEARTWOOD_LOCAL_MODEL_PATH=/home/jupyter/heartwood-workspace/models/qwen25-7b-instruct-q4_k_m/Qwen2.5-7B-Instruct-Q4_K_M.gguf \
  bash images/generic/scripts/start_local_runtime.sh
```

In another terminal, discover and select the model reported by the loopback runtime:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models refresh local
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models connect \
  local <model-id>
```

Choose a machine and disk based on the selected model manifest. The current reviewed Qwen artifact records 4 vCPU and 16 GB RAM as a minimum demonstration envelope and 8 vCPU and 32 GB RAM as the recommended envelope. It is CPU-only in the baseline runtime; attaching a GPU does not accelerate it.

## Start The Web Interface

In a terminal:

```bash
cd /opt/heartwood
HEARTWOOD_WORKSPACE=/home/jupyter/heartwood-workspace/sessions \
  HEARTWOOD_WEB_HOST=127.0.0.1 \
  HEARTWOOD_WEB_PORT=8767 \
  bash images/generic/scripts/start_web_ui.sh
```

Open the Jupyter proxy route for port `8767`. The browser path normally ends in `/proxy/8767/`; Heartwood infers that prefix while the internal gateway remains root-relative.

Open the existing `terra-demo` session and confirm in Settings that the selected model appears under the platform, local, or cloud connection used above. Submit these tasks in order:

1. `Build the synthetic target-condition cohort for concept 201826 with the repository-verified cohort Skill. Read the localized tables in input, require age 18 or older, apply an aggregate count floor of 20, write cohort-summary.json, and report the cohort definition and quality checks without row-level values.`
2. `Fit the repository-verified training-only age-only baseline for recorded condition 201826 history in the localized synthetic tables. Write baseline-model.json and report aggregate training diagnostics, the lack of holdout evaluation, and that this is not a clinical model.`
3. `Prepare the aggregate export from cohort-summary.json with the repository-verified aggregate export Skill. Apply a count floor of 20, write aggregate-export.json, and do not include row-level values.`

Review each proposed terminal or file action and select **Allow once** only when its command, paths, and expected output match the request. Exercise **Reject** in a separate synthetic session so the reference artifacts remain complete. The expected cohort output reports 24 source participants, 39 source condition rows, 20 cohort participants, 35 cohort condition rows, 20 target-condition occurrences, passing identifier, chronology, and referential-integrity checks, and no row values. The baseline output must identify itself as training-only with no holdout evaluation. The export output must report a count floor of 20, `exported: true`, and `suppressed: false`; successful script output is not an authorization to move data outside the workspace.

Verify that agent messages and actual tool outcomes appear in the conversation, then use Activity for the ordered event trace and Export Audit for the scrubbed record. Do not expect model route or repository-verified Skill activation prompts; those are deployment and installation decisions, not conversational action confirmation.

Action confirmation defaults to **Ask Every Time**. To validate the deployment-allowed risk-based path with synthetic data, select **Auto-Approve Low Risk** in Settings or run `heartwood --workspace /home/jupyter/heartwood-workspace/sessions actions set auto-approve-low-risk`. Confirm that low-risk actions still appear in Activity and that medium-, high-, and unknown-risk actions retain Allow once and Reject controls.

## Verify CLI And Notebook Parity

Use the same session identifier shown in the web UI:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions \
  --session-id terra-demo replay
heartwood --workspace /home/jupyter/heartwood-workspace/sessions \
  --session-id terra-demo audit export \
  --output /home/jupyter/heartwood-workspace/audit.jsonl
```

In the Heartwood kernel:

```python
from pathlib import Path
from heartwood.notebook import NotebookSession, jupyter_proxy_url

session = NotebookSession(
    workspace=Path("/home/jupyter/heartwood-workspace/sessions"),
    session_id="terra-demo",
)
view = session.replay()
print(view.event_count)
print(jupyter_proxy_url(port=8767))
```

The CLI, notebook bridge, and web UI must report the same persisted event count because they share one gateway contract and audit store. Run these checks sequentially after the web conversation is idle; concurrent independent processes writing the same session are not part of the current pre-release contract.

## Live-Validation Evidence

Record only synthetic evidence:

- custom image and base image digests;
- machine shape, disk size, startup time, and autopause/resume result;
- notebook route, Heartwood kernel, and proxy URL behavior;
- selected profile identifier, credential-reference kind, and policy decision without secret values;
- optional model artifact identifier and digest;
- one web conversation with a proposed action and its allow or reject result;
- CLI and notebook replay counts;
- scrubbed audit export path;
- observed runtime network policy and identity mechanism.

A real Terra workspace validation remains required before claiming the image is supported for a specific institutional deployment.

## Authoritative Terra References

- [Terra custom cloud environment tutorial](https://support.terra.bio/hc/en-us/articles/360037143432-Docker-tutorial-Custom-Cloud-Environments-for-Jupyter-Notebooks)
- [Terra Jupyter cloud environment customization](https://support.terra.bio/hc/en-us/articles/5075814468379-Starting-and-customizing-your-Jupyter-app)
- [Terra architecture and persistent-disk mounts](https://support.terra.bio/hc/en-us/articles/360058163311-Terra-architecture-where-your-data-and-tools-live)
- [Accessing workspace-bucket data from a notebook](https://support.terra.bio/hc/en-us/articles/360046617372-Accessing-data-from-the-workspace-Bucket-in-a-notebook)
- [Managing Terra data with tables](https://support.terra.bio/hc/en-us/articles/360025758392-Managing-data-with-tables)
- [Terra custom-environment base-image update](https://support.terra.bio/hc/en-us/articles/31191625622811-Easily-build-customize-and-reuse-compute-environments-Jupyter-Notebooks-Launching-1-16-26)
- [DataBiosphere Terra Docker image catalog](https://github.com/DataBiosphere/terra-docker)
