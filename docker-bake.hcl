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

variable "IMAGE_TAG_SUFFIX" {
  default = ""
}

variable "TERRA_BASE_IMAGE" {
  default = "us.gcr.io/broad-dsp-gcr-public/terra-jupyter-python:1.1.6"
}

variable "TERRA_BASE_PLATFORM" {
  default = "linux/amd64"
}

variable "TERRA_CI_BASE_IMAGE" {
  default = "heartwood-terra-ci-base:local"
}

group "default" {
  targets = ["runtime"]
}

target "_common" {
  context = "."
  dockerfile = "images/generic/Dockerfile"
  platforms = ["linux/amd64", "linux/arm64"]
  pull = true
  cache-from = ["type=gha"]
  cache-to = ["type=gha,mode=min"]
  attest = ["type=sbom", "type=provenance,mode=max"]
}

target "runtime" {
  inherits = ["_common"]
  args = {
    HEARTWOOD_BUNDLE_LOCAL_MODEL = "0"
    HEARTWOOD_IMAGE_FLAVOR = "runtime"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}${IMAGE_TAG_SUFFIX}",
    "${IMAGE_NAME}:sha-${GIT_SHA}${IMAGE_TAG_SUFFIX}",
  ]
}

target "smoke" {
  inherits = ["_common"]
  args = {
    HEARTWOOD_BUNDLE_LOCAL_MODEL = "1"
    HEARTWOOD_IMAGE_FLAVOR = "smoke"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-smoke${IMAGE_TAG_SUFFIX}",
    "${IMAGE_NAME}:sha-${GIT_SHA}-smoke${IMAGE_TAG_SUFFIX}",
  ]
}

target "providers" {
  inherits = ["_common"]
  args = {
    HEARTWOOD_BUNDLE_LOCAL_MODEL = "0"
    HEARTWOOD_IMAGE_FLAVOR = "providers"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-providers${IMAGE_TAG_SUFFIX}",
    "${IMAGE_NAME}:sha-${GIT_SHA}-providers${IMAGE_TAG_SUFFIX}",
  ]
}

target "_platform_common" {
  context = "."
  dockerfile = "images/platform/Dockerfile"
  pull = true
  cache-from = ["type=gha"]
  cache-to = ["type=gha,mode=min"]
}

target "_terra_common" {
  inherits = ["_platform_common"]
  platforms = ["linux/amd64"]
  args = {
    HEARTWOOD_PLATFORM = "terra"
    HEARTWOOD_PLATFORM_BASE_IMAGE = "${TERRA_BASE_IMAGE}"
    HEARTWOOD_PLATFORM_BASE_PLATFORM = "${TERRA_BASE_PLATFORM}"
    HEARTWOOD_RUNTIME_ARCH = "amd64"
    HEARTWOOD_PLATFORM_HOME = "/home/jupyter"
    HEARTWOOD_PLATFORM_USER = "jupyter"
    HEARTWOOD_JUPYTER_PREFIX = "/opt/conda"
  }
}

target "terra-runtime" {
  inherits = ["_terra_common"]
  attest = ["type=sbom", "type=provenance,mode=max"]
  args = {
    HEARTWOOD_BUNDLE_LOCAL_MODEL = "0"
    HEARTWOOD_IMAGE_FLAVOR = "terra-runtime"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra${IMAGE_TAG_SUFFIX}",
    "${IMAGE_NAME}:sha-${GIT_SHA}-terra${IMAGE_TAG_SUFFIX}",
  ]
}

target "terra-smoke" {
  inherits = ["_terra_common"]
  attest = ["type=sbom", "type=provenance,mode=max"]
  args = {
    HEARTWOOD_BUNDLE_LOCAL_MODEL = "1"
    HEARTWOOD_IMAGE_FLAVOR = "terra-smoke"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-smoke${IMAGE_TAG_SUFFIX}",
    "${IMAGE_NAME}:sha-${GIT_SHA}-terra-smoke${IMAGE_TAG_SUFFIX}",
  ]
}

target "terra-smoke-ci" {
  inherits = ["_terra_common"]
  pull = false
  args = {
    HEARTWOOD_PLATFORM_BASE_IMAGE = "${TERRA_CI_BASE_IMAGE}"
    HEARTWOOD_PLATFORM_BASE_PLATFORM = "linux/amd64"
    HEARTWOOD_BUNDLE_LOCAL_MODEL = "1"
    HEARTWOOD_IMAGE_FLAVOR = "terra-smoke-ci"
  }
  tags = [
    "${IMAGE_NAME}:${IMAGE_CHANNEL}-terra-smoke-ci${IMAGE_TAG_SUFFIX}",
  ]
}
