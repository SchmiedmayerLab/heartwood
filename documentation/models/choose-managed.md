<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Choose a Heartwood-Managed Model

Heartwood-managed inference runs on the same compute environment as Heartwood rather than sending requests to a separate model provider.
It requires compatible model files, sufficient storage and memory, and an agent-capable chat format.
Heartwood supports a small release-pinned recommendation set and best-effort planning for other public Hugging Face repositories.

## Recommended Models

Choose **Run with Heartwood** during setup to see the current recommendations.
Each recommendation includes the canonical Hugging Face repository, immutable revision, artifact format, license metadata, size, context capacity, and minimum and recommended resources.
Heartwood shows the expected download size before any transfer begins.

Recommendations are an onboarding aid, not a closed allowlist and not a guarantee of scientific suitability.
They are selected for the supported runtime and documented tool-use checks in the release.

List them from the terminal:

```bash
heartwood models managed
```

## Other Hugging Face Models

Choose **Other Hugging Face model** or enter an `owner/model` identifier:

```bash
heartwood models inspect unsloth/Qwen2.5-Coder-7B-Instruct-GGUF
heartwood models download unsloth/Qwen2.5-Coder-7B-Instruct-GGUF
```

In the browser, expand **Version options** to request a tag, branch, or commit. In the terminal, pass `--revision REVISION` to `models inspect` or `models download`. Heartwood records the immutable revision resolved by Hugging Face before download.

Heartwood asks Hugging Face for repository metadata, resolves an immutable revision, and looks for a supported candidate.
It prefers a balanced single-file GGUF for the portable llama.cpp CPU runtime or a safetensors snapshot for vLLM when an NVIDIA runtime is available.
The example above provides a GGUF candidate for the standard CPU image; a repository that contains only safetensors requires a GPU deployment with vLLM.

The current planner rejects repositories that require executable remote model code, unsupported weight layouts, missing architecture metadata, unresolved revisions, or an unsupported context range.
When a compatible-looking repository cannot be planned, the error explains the reason and links to the [Heartwood issue form](https://github.com/SchmiedmayerLab/heartwood/issues/new/choose).
The message states that the model is not yet supported rather than suggesting an unsafe manual workaround.

## What to Evaluate

Before downloading, review:

- **Tool use:** the model needs reliable structured tool calling, not only code completion.
- **License:** confirm that the model and weights are permitted for the intended work.
- **Provenance:** retain the repository and immutable revision with the managed artifact.
- **Format:** use one GGUF file for llama.cpp or a standard safetensors snapshot for vLLM.
- **Disk:** allow space for the model, download staging, runtime files, and project outputs.
- **Memory:** model weights, runtime overhead, and the key/value cache must fit in RAM or GPU memory.
- **Context:** larger windows consume substantially more memory and do not automatically improve task quality.
- **Data policy:** in-environment execution still depends on the security of the compute, storage, and surrounding platform.

Parameter count alone does not predict coding-agent quality or memory use.
Quantization, architecture, context window, concurrency, and runtime all matter.

## Import an Existing Model

When model files were transferred through an approved offline process, import them into the project:

```bash
heartwood models import /approved/path/model.gguf \
  --source owner/model \
  --revision 0123456789abcdef0123456789abcdef01234567 \
  --license apache-2.0
```

Heartwood accepts a valid GGUF file or a standard vLLM safetensors directory, rejects symbolic links and executable Python, records provenance, copies the artifact atomically into `.heartwood/models/`, and selects it.
The source path must be visible to the Heartwood process; the browser uses the same server-side import and does not upload multi-gigabyte model files through the page.
