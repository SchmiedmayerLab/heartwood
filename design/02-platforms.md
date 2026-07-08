<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# 02 — Platforms

## Shared model

Every target environment splits into a **control plane** (web app, data catalog, auth) and a **compute plane** (VMs that run code). The compute plane has two lanes, and Docker is the unit in both:

- **Interactive lane** — a long-lived VM that boots a container you type into (Jupyter / RStudio / shell); state persists on an attached disk; it autopauses when idle. **heartwood runs here.**
- **Batch lane** — a workflow engine (Cromwell / a CWL executor / Nextflow) that runs an ephemeral container per task. heartwood *emits* code for this lane; it does not live in it.

## Target environments

### Terra / All of Us / AnVIL
Runs on GCP and Azure. The **Leonardo** service provisions a Cloud Environment — a VM that boots a Docker container running Jupyter/RStudio, with `/home` on a detachable disk that survives autopause. Optional Dataproc/Spark (for Hail) and GPU. Custom interactive images **extend a Terra base image** (`terra-jupyter-python`, etc.); home is `/home/jupyter`; images come from GAR/GCR, GHCR, or public Docker Hub. All of Us curated data (the CDR) is OMOP-derived in **BigQuery**, wrapped by a **VPC Service Controls** perimeter. Public PyPI is blocked; images must be self-contained.

### Seven Bridges / Velsera (CGC, Cavatica, BioData Catalyst)
CWL-first, primarily AWS. **Data Studio** launches JupyterLab/RStudio on a chosen instance — the interactive home here. Batch tools pair a Docker image with a CWL description that maps input/output ports. Images live in the Seven Bridges Image Registry (`images.sbgenomics.com`).

### DNAnexus / UK Biobank RAP
Runs on AWS; compute is apps/applets. Interactive jobs expose a browser UI via an HTTPS proxy; **JupyterLab runs inside a Docker container** (a session snapshot is a tarball of the container). Docker is loaded via `docker pull` or, offline, `docker save` → a DNAnexus Asset → `docker load`. **Network is off by default** unless a job explicitly requests it. DNAnexus's own **Omics Data Agent** reaches models compliantly via **Bedrock over VPC endpoints / PrivateLink** (no public egress, in-region, under a BAA) — the same in-perimeter pattern heartwood uses.

### Generic
Any Linux/Jupyter VM. The generic adapter runs heartwood without platform lock-in and is the baseline for development and self-hosted TREs.

## In-boundary models

Egress is blocked, so models are reached **inside the perimeter**:

| Cloud | In-perimeter model path | Mechanism |
|---|---|---|
| GCP (Terra / All of Us) | Claude & Gemini on **Vertex AI** | VPC-SC + Private Service Connect, CMEK, audit logs |
| Azure (Terra-Azure) | **Azure OpenAI** | BAA, in-region, no training on data, VNet + private endpoint |
| AWS (Seven Bridges / DNAnexus) | **Bedrock** | PrivateLink — traffic stays in the AWS network |
| Any | **Local** vLLM / Ollama | Runs in-container/cluster; zero egress |

One LiteLLM configuration routes across all four; a per-platform policy profile flips between "local-only" and "in-perimeter endpoint."

## Data-use policy (a hard constraint)

Technical reachability is not permission. Flagship datasets constrain the use case:

- **All of Us** prohibits sending individual-level data to external AI/ML APIs; only aggregate results (subject to the **≥20-participant** count floor) may leave the perimeter; models trained on participant-level data may not be disseminated.
- **UK Biobank RAP** permits trained-model use but constrains outputs to minimum-count rules.

Consequences, built into the platform: **in-boundary-only is the default**, the model layer **provably blocks non-compliant egress**, aggregate-export skills enforce the count floor, and an **egress attestation** is produced for review.

## Portability (batch lane)

Emitted pipelines target **Dockstore** over **GA4GH DRS/WES/TES/TRS**: a pinned-image CWL/WDL/Nextflow workflow reading inputs via DRS runs across all platforms. This shapes how emitted code is structured.

## Embedding

One air-gapped image per platform variant (built from the platform adapter's base; Terra variant extends `terra-jupyter-python`), with dependencies and verified skills vendored and signature-checked at build time.
