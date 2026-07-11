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

## Configure A Model

The image contains no model weights. Choose one path.

For an institution-authorized endpoint, open a terminal and create a profile whose secret is supplied through the workspace environment, mounted secret file, or managed identity:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models add institutional \
  --model <litellm-provider>/<model-name> \
  --policy-endpoint https://<approved-endpoint>/<route> \
  --credential-kind environment \
  --api-key-env <RUNTIME_SECRET_ENV> \
  --select
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models validate institutional
```

The workspace must supply `HEARTWOOD_POLICY_PROFILE` with the exact approved endpoint, capability tier, and non-secret credential reference such as the environment-variable name. A provider name does not establish HIPAA eligibility or a business associate agreement.

For a local synthetic demo, list and download a reviewed artifact to Terra-persistent storage:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models artifacts
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models download \
  qwen25-coder-7b-instruct-q4_k_m \
  --cache /home/jupyter/heartwood-workspace/models
```

Start the included CPU server in a terminal using the exact path printed by the download:

```bash
cd /opt/heartwood
HEARTWOOD_LOCAL_MODEL_PATH=/home/jupyter/heartwood-workspace/models/qwen25-coder-7b-instruct-q4_k_m/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf \
  bash images/generic/scripts/start_local_runtime.sh
```

In another terminal, add the loopback profile:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models add local \
  --model openai/local-model \
  --base-url http://127.0.0.1:8765/v1 \
  --policy-endpoint http://127.0.0.1:8765/v1/chat/completions \
  --credential-kind none \
  --select
heartwood --workspace /home/jupyter/heartwood-workspace/sessions models validate local
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

Submit a synthetic coding task in the conversation. Verify that agent messages appear, a proposed terminal or file action appears inline, and Allow once or Reject updates the same conversation. Use Activity for the event trace and Export Audit for the scrubbed audit record. Do not expect model route or repository-verified Skill activation prompts; those are deployment and installation decisions, not conversational action confirmation.

Action confirmation defaults to **Ask Every Time**. To validate the deployment-allowed risk-based path with synthetic data, select **Auto-Approve Low Risk** in Settings or run `heartwood --workspace /home/jupyter/heartwood-workspace/sessions actions set auto-approve-low-risk`. Confirm that low-risk actions still appear in Activity and that medium-, high-, and unknown-risk actions retain Allow once and Reject controls.

## Verify CLI And Notebook Parity

Use the same session identifier shown in the web UI:

```bash
heartwood --workspace /home/jupyter/heartwood-workspace/sessions \
  --session-id session-local replay
heartwood --workspace /home/jupyter/heartwood-workspace/sessions \
  --session-id session-local audit export \
  --output /home/jupyter/heartwood-workspace/audit.jsonl
```

In the Heartwood kernel:

```python
from pathlib import Path
from heartwood.notebook import NotebookSession, jupyter_proxy_url

session = NotebookSession(
    workspace=Path("/home/jupyter/heartwood-workspace/sessions"),
    session_id="session-local",
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
- [DataBiosphere Terra Docker image catalog](https://github.com/DataBiosphere/terra-docker)
