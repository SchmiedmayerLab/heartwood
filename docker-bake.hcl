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
