<!--
This source file is part of the Heartwood open-source project
SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
SPDX-License-Identifier: MIT
-->

# Glossary

**Action set**
: One or more tool calls supplied through a single OpenHands confirmation callback and resolved together by Heartwood.

**Agent**
: Software that uses a model and tools iteratively to pursue a task rather than returning only one text response.

**Application programming interface (API)**
: A structured way for software components to communicate.

**Audit checkpoint**
: A canonical audit export signed by a deployment and bound to its session, origin, creation time, and retention declaration.

**Audit export**
: A scrubbed JSON Lines representation of the tamper-evident session audit chain.

**Blacksmith**
: A hosted GitHub Actions runner service Heartwood uses for architecture-matched, compute-intensive validation jobs.

**Carina**
: Stanford's research-computing platform using project storage and Slurm-managed compute.

**ChatGPT subscription connection**
: Account-based access to supported Codex models through OpenHands, separate from OpenAI API billing and API keys.

**Codex Responses API**
: The Responses API transport used by OpenHands for supported models reached through a ChatGPT subscription connection.

**Container**
: A packaged Linux filesystem and process environment run by software such as Docker.

**Context window**
: The token budget available to a model for instructions, conversation history, project content, tool results, and output.

**Credential binding**
: A non-secret identifier that tells Heartwood where an authorized process or platform supplies a provider credential.

**CUDA**
: NVIDIA's software platform and application binary interface for GPU computing.

**Device-code sign-in**
: An OAuth flow in which one interface displays a short-lived code that the user enters on a separate provider sign-in page.

**Ed25519**
: A public-key signature algorithm Heartwood uses to authenticate deployment-owned audit checkpoints.

**GGUF**
: A single-file model format commonly used with llama.cpp and quantized CPU inference.

**Graphics processing unit (GPU)**
: Specialized compute hardware used to accelerate model inference.

**Hugging Face**
: A model repository service used by Heartwood to inspect public metadata and download supported model artifacts.

**Heartwood-managed model**
: A model whose files and inference runtime Heartwood manages on the compute environment where Heartwood is running, such as a workstation container, Terra runtime, or Carina allocation.

**Ingress**
: The validated network route through which a browser or API client reaches the Heartwood gateway, such as direct loopback, a Jupyter proxy, or an explicitly trusted platform proxy.

**JSON Lines**
: A text format that stores one JSON value on each line so records can be appended and processed incrementally.

**Large language model (LLM)**
: The model used by the agent to interpret requests, reason, produce text, and propose tools.

**Model credential boundary**
: The technical separation, or documented lack of separation, between provider authentication used for model calls and the operating-system identity that runs agent tools.

**OAuth**
: A standard authorization protocol that lets a user grant account access without giving Heartwood the account password.

**One-time code**
: The short-lived code displayed during device-code sign-in and entered on the provider's verification page.

**OpenAI-compatible service**
: A model endpoint implementing the relevant OpenAI API request and response shapes; compatibility does not imply operation by OpenAI.

**OpenHands**
: The upstream agent SDK and coding-tool platform used by Heartwood for conversations and tool execution.

**Protected health information (PHI)**
: Individually identifiable health information protected under applicable policy or law.

**Project**
: The process current directory and its descendants that Heartwood treats as the agent workspace.

**Project state**
: Private non-secret configuration, sessions, models, Skills, logs, caches, runtime files, and audit artifacts under `.heartwood/`.

**Quantization**
: A lower-precision model representation, such as AWQ, GPTQ, or FP8, used to reduce memory and storage requirements.

**Research Skill**
: A versioned instruction package with declared tools, metadata, and workflow guidance available to the OpenHands agent.

**Responses API**
: OpenAI's current API shape for model responses, tool calls, and related agent interactions.

**SHA-256**
: A cryptographic hash function Heartwood uses to verify that model files and other artifacts have not changed.

**Slurm**
: A scheduler that allocates compute resources for jobs on platforms such as Stanford Carina.

**Tensor parallelism**
: A runtime layout that divides one model across a fixed number of GPUs.

**Terra**
: A cloud platform for biomedical research workspaces, data, workflows, and interactive Jupyter compute.

**vLLM**
: A GPU-oriented inference server used by supported Heartwood NVIDIA deployments.
