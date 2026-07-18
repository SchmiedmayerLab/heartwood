<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Glossary and Acronyms

This reference defines the specialized terms used in Heartwood's user and architecture documentation.

## Heartwood and Agent Terms

| Term | Meaning |
|---|---|
| AI | Artificial intelligence. Heartwood uses a language model to interpret requests and propose coding actions. |
| API | Application programming interface used by software components to communicate. |
| CLI | Command-line interface. Heartwood's terminal command is `heartwood`. |
| LLM | Large language model used by the OpenHands agent runtime. |
| LiteLLM | Provider-compatibility library used through OpenHands. |
| OpenHands | Upstream SDK that owns Heartwood's agent conversation, coding tools, confirmation stops, persistence, and context condensation. |
| SDK | Software development kit. Heartwood integrates the public OpenHands SDK rather than maintaining another agent loop. |
| Skill | A reviewed `SKILL.md` procedure, with optional scripts and resources, supplied to the OpenHands agent context. |
| UI | User interface, including Heartwood's terminal and browser presentations. |
| vLLM | GPU-oriented model server used by Heartwood's NVIDIA and Carina local-inference paths. |

## Research and Security Terms

| Term | Expansion | Meaning |
|---|---|---|
| BAA | Business Associate Agreement | Contract that can govern a service provider's handling of protected health information. |
| CDM | Common Data Model | Standardized schema; Heartwood's synthetic reference fixtures resemble a small subset of the OMOP CDM. |
| HIPAA | Health Insurance Portability and Accountability Act | US legal framework that includes privacy and security requirements for protected health information. |
| IRB | Institutional Review Board | Body responsible for oversight of human-subjects research. |
| OMOP | Observational Medical Outcomes Partnership | Community data model used by Heartwood's synthetic reference workflow. |
| PHI | Protected Health Information | Individually identifiable health information protected under applicable US regulation. |

## Models and Compute

| Term | Expansion | Meaning |
|---|---|---|
| AMD64 | 64-bit x86 architecture | Architecture used by common Linux servers and Heartwood's NVIDIA images. |
| ARM64 | 64-bit Arm architecture | Architecture used by Apple Silicon and Arm Linux systems. |
| AWQ | Activation-aware Weight Quantization | Quantization format supported by selected vLLM model artifacts. |
| CPU | Central Processing Unit | General-purpose processor used by the portable local-inference image. |
| CUDA | Compute Unified Device Architecture | NVIDIA software stack used by GPU inference. |
| GGUF | GPT-Generated Unified Format | Model file format used by Heartwood's llama.cpp path. |
| GPU | Graphics Processing Unit | Accelerator used by Heartwood's NVIDIA local-inference paths. |
| llama.cpp | Open-source inference project | Local model runtime used by Heartwood's portable CPU images. |
| RAM | Random Access Memory | Main system memory used by Heartwood and local inference. |
| Slurm | Cluster workload manager | Scheduler used to request Carina compute. |
| vCPU | Virtual Central Processing Unit | Compute-unit label used by cloud environments such as Terra. |
| VRAM | Video Random Access Memory | GPU memory used for model weights, context, and inference buffers. |

## Software, Data, and Deployment

| Term | Expansion | Meaning |
|---|---|---|
| ASGI | Asynchronous Server Gateway Interface | Python interface used by Heartwood's web gateway. |
| CI | Continuous Integration | Automated repository checks for code, documentation, images, and release artifacts. |
| GHCR | GitHub Container Registry | Registry that publishes Heartwood container images. |
| HTTP / HTTPS | Hypertext Transfer Protocol / HTTP Secure | Protocols used by model and browser APIs; remote model routes require HTTPS. |
| JSON | JavaScript Object Notation | Structured-data format used by Heartwood contracts and configuration. |
| JSONL | JSON Lines | One JSON object per line, used for audit exports and event storage. |
| OCI | Open Container Initiative | Standards used for container images and registry manifests. |
| REST | Representational State Transfer | HTTP API style used by the Heartwood session gateway. |
| REUSE | Software licensing specification and tool | Checks file-level copyright and license declarations. |
| SHA-256 | Secure Hash Algorithm 256-bit | Digest used to verify released and downloaded artifacts. |
| SPDX | Software Package Data Exchange | Standard identifiers used for source-file license declarations. |
| SSH | Secure Shell | Encrypted remote terminal protocol used to access environments such as Carina. |
| TOML | Tom's Obvious Minimal Language | Configuration format used for project and release metadata. |
| UID / GID | User Identifier / Group Identifier | Numeric Unix identities used to map container file ownership to the host user. |
