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

**Audit export**
: A scrubbed JSON Lines representation of the tamper-evident session audit chain.

**Blacksmith**
: A hosted GitHub Actions runner service Heartwood uses for architecture-matched, compute-intensive validation jobs.

**Carina**
: Stanford's research-computing platform using project storage and Slurm-managed compute.

**Container**
: A packaged Linux filesystem and process environment run by software such as Docker.

**Context window**
: The token budget available to a model for instructions, conversation history, project content, tool results, and output.

**Credential binding**
: A non-secret identifier that tells Heartwood where an authorized process or platform supplies a provider credential.

**CUDA**
: NVIDIA's software platform and application binary interface for GPU computing.

**GGUF**
: A single-file model format commonly used with llama.cpp and quantized CPU inference.

**Graphics processing unit (GPU)**
: Specialized compute hardware used to accelerate model inference.

**Hugging Face**
: A model repository service used by Heartwood to inspect public metadata and download supported model artifacts.

**Heartwood-managed model**
: A model whose files and inference runtime Heartwood manages on the compute environment where Heartwood is running, such as a workstation container, Terra runtime, or Carina allocation.

**Large language model (LLM)**
: The model used by the agent to interpret requests, reason, produce text, and propose tools.

**OpenHands**
: The upstream agent SDK and coding-tool platform used by Heartwood for conversations and tool execution.

**OpenAI-compatible service**
: A model endpoint implementing the relevant OpenAI API request and response shapes; compatibility does not imply operation by OpenAI.

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

**Slurm**
: A scheduler that allocates compute resources for jobs on platforms such as Stanford Carina.

**Tensor parallelism**
: A runtime layout that divides one model across a fixed number of GPUs.

**Terra**
: A cloud platform for biomedical research workspaces, data, workflows, and interactive Jupyter compute.

**vLLM**
: A GPU-oriented inference server used by supported Heartwood NVIDIA deployments.
