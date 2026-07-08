<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Acronyms & glossary

A running reference for the acronyms and named tools used across this project (medical data, genomics, cloud platforms, and agent tooling).

**Maintenance:** this file is kept up to date per the rule in [AGENTS.md](./AGENTS.md) — whenever a new acronym is introduced anywhere in the project, add it here with its expansion and a one-line description.

---

## Workflow languages & engines (the "batch lane")

| Term | Expansion | What it is |
|---|---|---|
| WDL | Workflow Description Language | Broad's pipeline language ("widdle"); executed by Cromwell |
| CWL | Common Workflow Language | Vendor-neutral open standard for pipelines/tools |
| Cromwell | *(name, not an acronym)* | Broad's engine that executes WDL/CWL |
| Nextflow | *(name)* | Popular workflow framework/DSL |
| Snakemake | *(name)* | Python-flavored workflow system |
| Galaxy | *(name)* | Web-based workflow platform |
| Hail | *(name)* | Genomics analysis framework that runs on Spark |
| Spark | *(name)* | Distributed compute engine for large datasets |

## GA4GH interoperability standards (the portability layer)

| Term | Expansion | What it is |
|---|---|---|
| GA4GH | Global Alliance for Genomics and Health | Standards body for genomic/health data sharing |
| DRS | Data Repository Service | Standard API to fetch a data object by ID, cloud-agnostic |
| WES | Workflow Execution Service | Standard API to submit a workflow to any platform |
| TES | Task Execution Service | Standard API to run one containerized task anywhere |
| TRS | Tool Registry Service | Standard API for sharing tools/workflows (used by Dockstore) |
| AAI | Authentication & Authorization Infrastructure | GA4GH "Passport/Visa" identity standard |

## Health-data & genomics standards / formats

| Term | Expansion | What it is |
|---|---|---|
| OMOP | Observational Medical Outcomes Partnership | The community that defined a common health-data schema |
| CDM | Common Data Model | The standardized schema itself (usually "OMOP CDM") |
| OHDSI | Observational Health Data Sciences and Informatics | Community/tooling around OMOP (pronounced "Odyssey") |
| QC | Quality Control | Checks that validate data, code, or analysis outputs before use |
| FHIR | Fast Healthcare Interoperability Resources | HL7's modern clinical-data exchange standard ("fire") |
| HL7 | Health Level Seven | The healthcare-standards organization |
| CDR | Curated Data Repository | All of Us's curated dataset (lives in BigQuery) |
| DICOM | Digital Imaging and Communications in Medicine | Medical imaging file/exchange standard |
| VCF | Variant Call Format | Text format listing genomic variants |
| BAM | Binary Alignment Map | Compressed file of sequencing reads aligned to a genome |
| GATK | Genome Analysis Toolkit | Broad's variant-calling toolkit |

## Compliance & security

| Term | Expansion | What it is |
|---|---|---|
| PHI | Protected Health Information | Identifiable health data (must not leak) |
| HIPAA | Health Insurance Portability and Accountability Act | US law governing PHI handling |
| BAA | Business Associate Agreement | Contract letting a vendor process PHI compliantly |
| FedRAMP | Federal Risk and Authorization Management Program | US government cloud-security authorization |
| VPC | Virtual Private Cloud | An isolated private network in a cloud |
| VPC-SC | VPC Service Controls | Google's "perimeter" that blocks data egress |
| IRB | Institutional Review Board | Ethics board that approves human-subjects research |
| DAC | Data Access Committee | Body that approves access to a controlled dataset |
| TRE | Trusted Research Environment | Secure enclave for sensitive data (e.g. UKB-RAP) |
| SLSA | Supply-chain Levels for Software Artifacts | Build/provenance integrity framework ("salsa") |
| SBOM | Software Bill of Materials | Inventory of components/dependencies in a build |
| CMEK | Customer-Managed Encryption Keys | Cloud encryption with customer-controlled keys |
| OIDC | OpenID Connect | Identity layer; used for CI "trusted publishing" of signed artifacts |
| SHA | Secure Hash Algorithm | Family of cryptographic hashes; SHA-256 is used for content pinning |
| SPDX | Software Package Data Exchange | Standard for per-file license/copyright metadata (used by REUSE) |
| TLS | Transport Layer Security | Encryption protocol used to secure network connections |

## Cloud & infrastructure

| Term | Expansion | What it is |
|---|---|---|
| GCP | Google Cloud Platform | Google's cloud |
| AWS | Amazon Web Services | Amazon's cloud |
| GCE | Google Compute Engine | GCP's virtual machines |
| GKE | Google Kubernetes Engine | GCP's managed Kubernetes |
| EBS | Elastic Block Store | AWS's attachable disk storage |
| VM | Virtual Machine | A virtualized computer instance |
| GPU | Graphics Processing Unit | Accelerator for ML / heavy compute |
| vCPU | virtual CPU | A virtual processor core on a VM |
| OS | Operating System | e.g. Linux / Ubuntu |
| DNS | Domain Name System | Resolves hostnames to network addresses |
| HTTP(S) | HyperText Transfer Protocol (Secure) | The web request protocol |
| PR | Pull Request | Proposed code or documentation change reviewed before merge |
| SSE | Server-Sent Events | One-way streaming over HTTP (used for live agent output) |
| PSC | Private Service Connect | GCP private connectivity to a service (no public internet) |
| PE | Private Endpoint | Azure private network entry to a service (no public ingress) |

## Container registries (where Docker images live)

| Term | Expansion |
|---|---|
| GCR | Google Container Registry |
| GAR | Google Artifact Registry |
| GHCR | GitHub Container Registry |
| OCI | Open Container Initiative (open standard for images & artifacts) |
| ORAS | OCI Registry As Storage (push/pull arbitrary artifacts via an OCI registry) |

## AI / agent stack

| Term | Expansion | What it is |
|---|---|---|
| AI | Artificial Intelligence | Broad term for systems that perform tasks associated with human intelligence |
| LLM | Large Language Model | The model (e.g. Claude) |
| MCP | Model Context Protocol | Open standard for giving agents tools/data |
| ML | Machine Learning | Statistical/modeling techniques that learn patterns from data |
| RAG | Retrieval-Augmented Generation | Feeding retrieved docs into the model to ground answers |
| NLP | Natural Language Processing | Computing over human-language text |
| NL | Natural Language | Plain human language (e.g. "NL query") |
| SQL | Structured Query Language | Language for querying databases ("sequel") |
| vLLM | *(project name)* | High-throughput engine for serving LLMs on GPUs |
| TGI | Text Generation Inference | Hugging Face's LLM serving engine |
| MLX | *(Apple framework; not a formal acronym)* | Apple-Silicon ML / array framework |
| LiteLLM / Ollama / llama.cpp | *(project names)* | Model-routing proxy / local model runners |
| ACP | Agent Client Protocol | Standard letting editors (Zed, JetBrains, VS Code) drive external agents, or one agent use another as a sub-agent/provider |
| SKILL.md | *(file name; open standard)* | Cross-vendor open standard (published Dec 2025) for packaging agent instructions/scripts as a discoverable folder |
| AAIF | Agentic AI Foundation | Linux Foundation body formed Dec 2025 to neutrally govern agent-ecosystem projects (MCP, Goose, AGENTS.md) |
| HITL | Human-in-the-Loop | Human approval/inspection points inside the agent loop |
| ADK | Agent Development Kit | Google's agent framework (evaluated, not chosen) |
| ODA | Omics Data Agent | DNAnexus's built-in GenAI cohort/query assistant (an in-boundary reference point) |

## Programming & interop

| Term | Expansion | What it is |
|---|---|---|
| API | Application Programming Interface | How programs talk to a service |
| CLI | Command-Line Interface | Terminal-driven tool |
| SDK | Software Development Kit | Libraries/tools for building on a platform |
| PyPI | Python Package Index | Public Python package registry; blocked at runtime in the air-gapped image |
| IDE | Integrated Development Environment | Code editor + tooling (VS Code, JetBrains) |
| JSON | JavaScript Object Notation | Lightweight structured-data format used for schemas and events |
| TOML | Tom's Obvious Minimal Language | Configuration format used for Python project metadata and the skill bundle catalog |
| YAML | YAML Ain't Markup Language | Human-readable configuration format used for workflows and metadata |
| NDJSON | Newline-Delimited JSON | One JSON record per line; the FHIR bulk-export format |
| FFI | Foreign Function Interface | Calling one language's code from another |
| ABI | Application Binary Interface | Binary calling convention (e.g. "C-ABI") |
| GIL | Global Interpreter Lock | Python's single-thread-at-a-time lock |
| ToS | Terms of Service | Legal usage terms (e.g. Anthropic's Commercial ToS governs the Claude Agent SDK, unlike MIT/Apache-licensed alternatives) |
| ADR | Architecture Decision Record | Short doc capturing one design decision + rationale |
| CI/CD | Continuous Integration / Continuous Delivery | Automated build/test/release pipeline |
| BYOD | Bring Your Own Data | A user's own dataset brought into a platform |
| SPI | Service Provider Interface | A stable interface a platform/model/data adapter implements |
| UI | User Interface | The screens and controls a person uses to operate software |
| UX | User Experience | The overall user workflow and interaction quality |

## Platforms & organizations

| Term | Expansion | What it is |
|---|---|---|
| UKB-RAP | UK Biobank – Research Analysis Platform | DNAnexus-based environment for UK Biobank |
| CGC | Cancer Genomics Cloud | Seven Bridges–powered platform |
| AnVIL | Analysis, Visualization, and Informatics Lab-space | NHGRI's Terra-based platform |
| NIH | National Institutes of Health | US biomedical research agency (runs All of Us) |
| NHLBI | National Heart, Lung, and Blood Institute | NIH institute behind BioData Catalyst |
| NHGRI | National Human Genome Research Institute | NIH institute behind AnVIL |

---

## Named tools & platforms that are *not* acronyms

Terra · Leonardo (Terra's environment service) · Dataproc (GCP managed Spark) · Seven Bridges · Velsera (its parent) · Cavatica · Dockstore · OpenHands / Goose / Aider (agent frameworks) — all product names.

Also evaluated as candidate agent-harness foundations: OpenCode (sst/Anomaly), Cline, Continue (Continue.dev), Codex CLI (OpenAI), SWE-agent / mini-SWE-agent and SWE-ReX (Princeton NLP), Claude Agent SDK (Anthropic) — all product names.

Orchestration/agent frameworks and durability engines evaluated: LangGraph, LlamaIndex Workflows, Pydantic AI, Google ADK, smolagents, AutoGen / AG2, CrewAI, DBOS, Temporal. Evaluation & skills-testing tooling: Inspect AI, promptfoo, DeepEval, SkillsBench, skillgrade; coding-ability benchmarks SWE-bench (Verified), Terminal-Bench, and LiveCodeBench. Supply-chain & runtime: Sigstore, bubblewrap. Data & registries: Synthea (synthetic health data), nb-cli (headless notebook driver), BioContextAI and GoekeLab awesome-genomic-skills (community skill/MCP registries) — all product/project names.

Development-practice tooling: Docker, Docker Compose, GitHub Actions, Dependabot, Codecov, Linkspector, Jupyter, JupyterLab, RStudio, ipywidgets, Node.js, actionlint, yamllint, REUSE (reuse.software — per-file SPDX licensing), ruff, and uv (Python) — all product/project names.
