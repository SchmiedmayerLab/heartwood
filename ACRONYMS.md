<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Acronyms and Glossary

This glossary defines acronyms and specialized terms used in Heartwood documentation. Add an entry when introducing a term and remove entries that are no longer used.

## Workflow Languages and Engines

| Term | Expansion | What it is |
|---|---|---|
| WDL | Workflow Description Language | Broad's pipeline language ("widdle"); executed by Cromwell |
| CWL | Common Workflow Language | Vendor-neutral open standard for pipelines/tools |
| Cromwell | *(name, not an acronym)* | Broad's engine that executes WDL/CWL |
| Nextflow | *(name)* | Popular workflow framework/DSL |
| Hail | *(name)* | Genomics analysis framework that runs on Spark |
| Spark | *(name)* | Distributed compute engine for large datasets |

## GA4GH Interoperability Standards

| Term | Expansion | What it is |
|---|---|---|
| GA4GH | Global Alliance for Genomics and Health | Standards body for genomic/health data sharing |
| DRS | Data Repository Service | Standard API to fetch a data object by ID, cloud-agnostic |
| WES | Workflow Execution Service | Standard API to submit a workflow to any platform |
| TES | Task Execution Service | Standard API to run one containerized task anywhere |
| TRS | Tool Registry Service | Standard API for sharing tools/workflows (used by Dockstore) |

## Health Data and Genomics Standards and Formats

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

## Compliance and Security

| Term | Expansion | What it is |
|---|---|---|
| PHI | Protected Health Information | Identifiable health data (must not leak) |
| HIPAA | Health Insurance Portability and Accountability Act | US law governing PHI handling |
| BAA | Business Associate Agreement | Contract letting a vendor process PHI compliantly |
| VPC | Virtual Private Cloud | An isolated private network in a cloud |
| VPC-SC | VPC Service Controls | Google's "perimeter" that blocks data egress |
| IRB | Institutional Review Board | Ethics board that approves human-subjects research |
| DAC | Data Access Committee | Body that approves access to a controlled dataset |
| TRE | Trusted Research Environment | Secure enclave for sensitive data (e.g. UKB-RAP) |
| SBOM | Software Bill of Materials | Inventory of components/dependencies in a build |
| SHA | Secure Hash Algorithm | Family of cryptographic hashes; SHA-256 is used for content pinning |
| SPDX | Software Package Data Exchange | Standard for per-file license/copyright metadata (used by REUSE) |
| TLS | Transport Layer Security | Encryption protocol used to secure network connections |

## Cloud and Infrastructure

| Term | Expansion | What it is |
|---|---|---|
| AMD64 | 64-bit Advanced Micro Devices architecture | x86-64 CPU architecture identifier used by Linux Docker images |
| ARM64 | 64-bit Arm architecture | CPU architecture identifier used by Apple Silicon, AWS Graviton, and Arm Linux runners |
| GCP | Google Cloud Platform | Google's cloud |
| AWS | Amazon Web Services | Amazon's cloud |
| VM | Virtual Machine | A virtualized computer instance |
| CPU | Central Processing Unit | General-purpose processor used for local inference when no accelerator is available |
| CUDA | Compute Unified Device Architecture | NVIDIA GPU runtime and programming stack used by optional accelerated inference profiles |
| GID | Group Identifier | Numeric Unix group id used to run containers as a stable non-root group |
| GPU | Graphics Processing Unit | Accelerator for ML / heavy compute |
| UID | User Identifier | Numeric Unix user id used to run containers as a stable non-root user |
| vCPU | virtual CPU | A virtual processor core on a VM |
| OS | Operating System | e.g. Linux / Ubuntu |
| CDN | Content Delivery Network | External static-asset hosting, forbidden for the runtime web UI |
| DNS | Domain Name System | Resolves hostnames to network addresses |
| HTTP(S) | HyperText Transfer Protocol (Secure) | The web request protocol |
| IPv6 | Internet Protocol version 6 | Network address format used by bracketed loopback and service endpoints |
| PR | Pull Request | Proposed code or documentation change reviewed before merge |
| RAM | Random Access Memory | Volatile memory required to load local model weights, runtime buffers, and active sessions |
| REST | Representational State Transfer | HTTP API style used by the session gateway |
| SSE | Server-Sent Events | One-way streaming over HTTP (fallback for live agent output) |
| SSH | Secure Shell | Encrypted remote terminal protocol used to access environments such as Carina |
| WS | WebSocket | Bidirectional streaming over one TCP connection (primary transport for live agent sessions) |
| PSC | Private Service Connect | GCP private connectivity to a service (no public internet) |
| PE | Private Endpoint | Azure private network entry to a service (no public ingress) |
| QEMU | Quick Emulator | CPU emulation layer used by container CI to run non-native architecture smoke tests |

## Container Registries

| Term | Expansion |
|---|---|
| GCR | Google Container Registry |
| GAR | Google Artifact Registry |
| GHCR | GitHub Container Registry |
| OCI | Open Container Initiative (open standard for images & artifacts) |

## AI and Agent Stack

| Term | Expansion | What it is |
|---|---|---|
| AI | Artificial Intelligence | Broad term for systems that perform tasks associated with human intelligence |
| LLM | Large Language Model | Model used by the OpenHands conversation runtime |
| GGUF | GPT-Generated Unified Format | llama.cpp model artifact format used for local inference profiles |
| ML | Machine Learning | Statistical/modeling techniques that learn patterns from data |
| NLP | Natural Language Processing | Computing over human-language text |
| NL | Natural Language | Plain human language (e.g. "NL query") |
| SQL | Structured Query Language | Language for querying databases ("sequel") |
| vLLM | *(project name)* | High-throughput engine for serving LLMs on GPUs |
| LiteLLM / Ollama / llama.cpp | *(project names)* | Model-routing proxy / local model runners |
| SKILL.md | *(file name)* | Portable directory format for agent instructions, scripts, references, and assets |

## Programming and Interoperability

| Term | Expansion | What it is |
|---|---|---|
| API | Application Programming Interface | How programs talk to a service |
| ASGI | Asynchronous Server Gateway Interface | Python interface used for async web servers and applications |
| CLI | Command-Line Interface | Terminal-driven tool |
| SDK | Software Development Kit | Libraries/tools for building on a platform |
| PyPI | Python Package Index | Public Python package registry; unavailable when a deployment disables runtime network access |
| JSON | JavaScript Object Notation | Lightweight structured-data format used for schemas and events |
| JSONL | JSON Lines | One JSON object per line, used for scrubbed audit exports |
| JIT | Just-In-Time | Compilation performed at runtime, including optional accelerator-kernel compilation |
| TOML | Tom's Obvious Minimal Language | Configuration format used for Python project metadata and the skill bundle catalog |
| YAML | YAML Ain't Markup Language | Human-readable configuration format used for workflows and metadata |
| NDJSON | Newline-Delimited JSON | One JSON record per line; the FHIR bulk-export format |
| CI/CD | Continuous Integration / Continuous Delivery | Automated build/test/release pipeline |
| SPI | Service Provider Interface | A stable interface a platform/model/data adapter implements |
| SPA | Single-Page Application | A web app that loads once and updates in place (the researcher web UI) |
| UI | User Interface | The screens and controls a person uses to operate software |
| UX | User Experience | The overall user workflow and interaction quality |

## Platforms and Organizations

| Term | Expansion | What it is |
|---|---|---|
| UKB-RAP | UK Biobank – Research Analysis Platform | DNAnexus-based environment for UK Biobank |
| CGC | Cancer Genomics Cloud | Seven Bridges–powered platform |
| AnVIL | Analysis, Visualization, and Informatics Lab-space | NHGRI's Terra-based platform |
| NIH | National Institutes of Health | US biomedical research agency (runs All of Us) |
| NHLBI | National Heart, Lung, and Blood Institute | NIH institute behind BioData Catalyst |
| NHGRI | National Human Genome Research Institute | NIH institute behind AnVIL |
