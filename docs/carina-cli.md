<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# Use Heartwood on Stanford Carina

Heartwood runs natively in a Carina terminal. It can use the Stanford AI API Gateway without requesting a GPU or launch a local model through an explicitly reviewed Slurm allocation.

Use an isolated project directory and begin with synthetic files. Do not inspect unrelated project directories or cluster data while learning the workflow.

## 1. Prepare Project Storage

Connect to Carina and enter the writable project storage assigned to the research project. Carina 2.0 project directories normally follow `/projects/<PI>/<projectID>/main`; use the exact path provided to your team.

Create separate installation and agent project directories:

```bash
cd /projects/<PI>/<projectID>/main
mkdir -p -m 700 heartwood-installation heartwood-demo
```

Keeping the installation beside the project prevents the agent from treating application runtimes as analysis files.

## 2. Install Heartwood

```bash
cd heartwood-installation
module load micromamba/2.3.3
curl --fail --location --output heartwood-installer \
  https://github.com/SchmiedmayerLab/heartwood/releases/download/0.2.0-beta.3/heartwood-installer
chmod +x heartwood-installer
./heartwood-installer --platform carina
rm heartwood-installer
export PATH="$PWD/bin:$PATH"
```

The installer uses the current directory as its root, verifies the release assets, and installs both Heartwood and its Carina vLLM runtime. Dependency resolution can take several minutes and reports seven numbered stages. A successful installation removes temporary installer state.

Enter the separate project:

```bash
cd ../heartwood-demo
heartwood --version
heartwood doctor
```

Before model setup, `heartwood doctor` reports `setup-required`.

After reconnecting, restore the command and return to the project:

```bash
cd /projects/<PI>/<projectID>/main/heartwood-installation
module load micromamba/2.3.3
export PATH="$PWD/bin:$PATH"
cd ../heartwood-demo
```

## 3. Choose a Model Path

### Stanford AI API Gateway

This path needs no model download or GPU allocation. Confirm that the project has access to the [Stanford AI API Gateway](https://uit.stanford.edu/service/ai-api-gateway), then run `heartwood`, choose **Stanford AI API Gateway**, enter the issued token at the private prompt, and select one of the aliases returned by the service.

The current Stanford service terms, GenAI Evaluation Matrix, Data Risk Assessment, project authorization, and Carina controls determine which data may use the route. Technical connectivity does not authorize agent tools or export.

### Local GPU Model

List compatible models:

```bash
heartwood models local
```

For the L40S hardware in Carina 2.0, the full synthetic-demo model can be prepared with:

```bash
heartwood models download qwen25-7b-instruct-vllm
```

Heartwood downloads the immutable public snapshot into the project, reports progress, verifies the files, and saves the selection. Review its storage and memory plan before transfer. The model is intended for synthetic tool-use demonstrations, not biomedical or production validation.

Preview the compute request without submitting it:

```bash
heartwood launch --dry-run
```

## 4. Launch the Local Model

From the project directory:

```bash
heartwood launch
```

Heartwood discovers Carina's `dev`, `normal`, and `long` GPU partitions and proposes an allocation. Review the partition, GPU, CPU, memory, time, project, and model before answering `y`. Use `--partition <name>` only when the detected default is not appropriate.

Inside the allocation, Heartwood stages the verified model to job-local scratch, starts vLLM, reports startup progress, validates the connection, and opens the terminal conversation. Exiting the conversation stops the model server, removes the scratch copy, and releases the interactive allocation.

Carina's login nodes are for setup and job submission, not model inference. Do not start vLLM directly on a login node.

## 5. Try a Synthetic Task

Use [Work with Heartwood](using-heartwood.md) to submit a bounded task in `heartwood-demo`. Review the complete action group, allow only the intended synthetic action, and separately exercise rejection. Replay and export the session before ending the allocation:

```bash
heartwood replay
heartwood audit export
```

The Carina interface is currently the terminal. No authenticated Heartwood browser route is documented for Carina.

## Carina Help

- [Connect to Carina](https://docs.carina.stanford.edu/connect)
- [Stanford AI API Gateway](https://uit.stanford.edu/service/ai-api-gateway)
- [Stanford GenAI Tool Evaluation Matrix](https://uit.stanford.edu/ai/genai-tool-matrix)
- [Slurm partitions and GPU requests](https://docs.carina.stanford.edu/slurm-carina)
- [Software modules](https://docs.carina.stanford.edu/software)
- [Carina troubleshooting](https://docs.carina.stanford.edu/troubleshooting)
- [Heartwood troubleshooting](troubleshooting.md)
