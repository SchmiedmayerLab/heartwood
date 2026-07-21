# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

variable "IMAGE_NAME" {
  default = "ghcr.io/schmiedmayerlab/heartwood"
}

variable "IMAGE_CHANNEL" {
  default = "edge"
}

variable "GIT_SHA" {
  default = "local"
}

variable "HEARTWOOD_VERSION" {
  default = "0.2.0-beta.4"
}

variable "TERRA_BASE_IMAGE" {
  default = "us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6"
}

variable "TERRA_CI_BASE_IMAGE" {
  default = "heartwood-terra-ci-base:local"
}

group "default" {
  targets = ["runtime"]
}

target "runtime-gpu-nvidia" {
  inherits = ["runtime"]
  platforms = ["linux/amd64"]
  cache-from = ["type=gha,scope=runtime-gpu-nvidia"]
  cache-to = ["type=gha,scope=runtime-gpu-nvidia,mode=min"]
  args = {
    HEARTWOOD_IMAGE_FLAVOR = "runtime-gpu-nvidia"
    HEARTWOOD_GPU_RUNTIME = "vllm"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-gpu-nvidia",
    "${IMAGE_NAME}:sha-${GIT_SHA}-gpu-nvidia",
  ]
}

target "runtime" {
  context = "."
  dockerfile = "images/Dockerfile"
  target = "runtime-image"
  platforms = ["linux/amd64", "linux/arm64"]
  pull = true
  cache-from = ["type=gha"]
  cache-to = ["type=gha,mode=min"]
  attest = ["type=sbom", "type=provenance,mode=max"]
  args = {
    HEARTWOOD_IMAGE_FLAVOR = "runtime"
    HEARTWOOD_VERSION = "${HEARTWOOD_VERSION}"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}",
    "${IMAGE_NAME}:sha-${GIT_SHA}",
  ]
}

target "_terra_common" {
  context = "."
  dockerfile = "images/Dockerfile"
  target = "platform-runtime-image"
  pull = true
  platforms = ["linux/amd64"]
  args = {
    HEARTWOOD_PLATFORM = "terra"
    HEARTWOOD_BASE_IMAGE = "${TERRA_BASE_IMAGE}"
    HEARTWOOD_BASE_PLATFORM = "linux/amd64"
    HEARTWOOD_RUNTIME_HOME = "/home/jupyter"
    HEARTWOOD_RUNTIME_USER = "jupyter"
    HEARTWOOD_WORKDIR = "/home/jupyter"
    HEARTWOOD_CREATE_USER = "false"
    HEARTWOOD_JUPYTER_PREFIX = "/opt/conda"
    HEARTWOOD_INSTALL_JUPYTER_KERNEL = "true"
    HEARTWOOD_UV_PYTHON_PREFERENCE = "managed"
    HEARTWOOD_VERSION = "${HEARTWOOD_VERSION}"
  }
}

target "terra-runtime" {
  inherits = ["_terra_common"]
  cache-from = ["type=gha"]
  cache-to = ["type=gha,mode=min"]
  output = ["type=registry,oci-mediatypes=false"]
  args = {
    HEARTWOOD_IMAGE_FLAVOR = "terra-runtime"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra",
    "${IMAGE_NAME}:sha-${GIT_SHA}-terra",
  ]
}

target "terra-runtime-gpu-nvidia" {
  inherits = ["_terra_common"]
  cache-from = ["type=gha,scope=terra-runtime-gpu-nvidia"]
  cache-to = ["type=gha,scope=terra-runtime-gpu-nvidia,mode=min"]
  output = ["type=registry,oci-mediatypes=false"]
  args = {
    HEARTWOOD_IMAGE_FLAVOR = "terra-runtime-gpu-nvidia"
    HEARTWOOD_GPU_RUNTIME = "vllm"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-gpu-nvidia",
    "${IMAGE_NAME}:sha-${GIT_SHA}-terra-gpu-nvidia",
  ]
}

target "terra-ci" {
  inherits = ["_terra_common"]
  pull = false
  args = {
    HEARTWOOD_BASE_IMAGE = "${TERRA_CI_BASE_IMAGE}"
    HEARTWOOD_IMAGE_FLAVOR = "terra-ci"
  }
  tags = ["${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-ci"]
}
