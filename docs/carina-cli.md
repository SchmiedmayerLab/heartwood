<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

% TODO: We should generally define a structure of colored boxes and formatting that we use consistenty across the documentation. We should document this somewhere (probably not user facing?). But could even be user-facing to understand how to read the documentation.

# Use Heartwood on Stanford Carina

% TODO: Carina Terminal is not the right term. Carina is the compute platoform; aim to use correct terminology here. Maybe even start with some generic information about Carina, what it is, and how to connect to it (or better link to Stanford documentation for htat).
Heartwood runs natively in a Carina terminal. It can use the Stanford AI API Gateway without requesting a GPU or launch a local model through an explicitly reviewed Slurm allocation.

% TODO: Maybe we should format thig as a tip/box/suggestion?
Use an isolated project directory and begin with synthetic files. Do not inspect unrelated project directories or cluster data while learning the workflow.

## 1. Prepare Project Storage

% TODO: I don't think that "/main" is part of the plan. But noted that one should create a folder there for the insallation and maybe don't name it "demo" but maybe "first example" or someting? Link ot an other documentation that explains the folder structure (I think we have a page there) and what the different between the installation and project folder is. We might even want to extend that page to gover that.
Connect to Carina and enter the writable project storage assigned to the research project. Carina 2.0 project directories normally follow `/projects/<PI>/<projectID>/main`; use the exact path provided to your team.

% TODO: Ideally explain the commands separately so it's clear for beginners what they do here ....
Create separate installation and agent project directories:

```bash
cd /projects/<PI>/<projectID>/main
mkdir -p -m 700 heartwood-installation heartwood-demo
```

Keeping the installation beside the project prevents the agent from treating application runtimes as analysis files.

## 2. Install Heartwood

% TODO: Provde some more context here, commands that are clusered should be exlained togehter. E..g the cleanup should be a seprate element ...?
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

% TODO: SHouldn't this be before the command. And be sure to explain what's happening and make it approachable.
The installer uses the current directory as its root, verifies the release assets, and installs both Heartwood and its Carina vLLM runtime. Dependency resolution can take several minutes and reports seven numbered stages. A successful installation removes temporary installer state.

% TOOD: This might be a separte top-level step here? Make this approachable.
Enter the separate project:

```bash
cd ../heartwood-demo
heartwood --version
heartwood doctor
```

Before model setup, `heartwood doctor` reports `setup-required`.

% TODO: I think this is actually important, maybe we would need to even link to this part above is someone already did the installation before?
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

% TODO: We need to link to the Stanford documentation here; 
The current Stanford service terms, GenAI Evaluation Matrix, Data Risk Assessment, project authorization, and Carina controls determine which data may use the route. 
% TODO: What does that need? Is that nescessary/make this more approachable. Use some better formatting like tip boxes.
Technical connectivity does not authorize agent tools or export.

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

% TODO: What is a user followed the Stanford API gateway setup above? Would that also work? And if that's the case, then "Local Model" above is not correct? If it only works for the local model we should change the documentation or even the implementation here, there should be one command to use the selected and configured way to launch heartwood; if there are multiple selected an interactive selectino (on the web and on the CLI/chat interface) should allow a selection on launch ...

From the project directory:

```bash
heartwood launch
```

% TODO: This is only relevant for local models?
Heartwood discovers Carina's `dev`, `normal`, and `long` GPU partitions and proposes an allocation. Review the partition, GPU, CPU, memory, time, project, and model before answering `y`. Use `--partition <name>` only when the detected default is not appropriate.

% TODO: This needs to be appraochable? 
Inside the allocation, Heartwood stages the verified model to job-local scratch, starts vLLM, reports startup progress, validates the connection, and opens the terminal conversation. Exiting the conversation stops the model server, removes the scratch copy, and releases the interactive allocation.

Carina's login nodes are for setup and job submission, not model inference. Do not start vLLM directly on a login node.

## 5. Try a Synthetic Task

Use [Work with Heartwood](using-heartwood.md) to submit a bounded task in `heartwood-demo`. Review the complete action group, allow only the intended synthetic action, and separately exercise rejection. Replay and export the session before ending the allocation:

% TODO: This is a bit out of context, on one knows what replay does ... this needs to be explained first, maybe even in the main launch page as this might be a relevant feature?
```bash
heartwood replay
heartwood audit export
```

The Carina interface is currently the terminal.
% TODO: This reads itself like an internal developer documentation and an AI model artifact; avoid sentences like this and ensure the documentation is user-facing ...
No authenticated Heartwood browser route is documented for Carina.

## Carina Help

- [Connect to Carina](https://docs.carina.stanford.edu/connect)
- [Stanford AI API Gateway](https://uit.stanford.edu/service/ai-api-gateway)
- [Stanford GenAI Tool Evaluation Matrix](https://uit.stanford.edu/ai/genai-tool-matrix)
- [Slurm partitions and GPU requests](https://docs.carina.stanford.edu/slurm-carina)
- [Software modules](https://docs.carina.stanford.edu/software)
- [Carina troubleshooting](https://docs.carina.stanford.edu/troubleshooting)
- [Heartwood troubleshooting](troubleshooting.md)
