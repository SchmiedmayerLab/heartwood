# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

variable "project_id" {
  description = "Google Cloud project that holds the cluster."
  type        = string
}

variable "region" {
  description = "Region of the network and subnet."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Zone of the cluster and its node pools. It must offer NVIDIA L4 GPUs."
  type        = string
  default     = "us-central1-a"
}

variable "cluster_name" {
  description = "Name of the cluster; also the prefix of the network, subnet and node service account."
  type        = string
  default     = "heartwood-reference"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,19}$", var.cluster_name))
    error_message = "cluster_name must start with a letter and have at most 20 lowercase letters, digits or hyphens."
  }
}

variable "system_machine_type" {
  description = "Machine type of the system node pool."
  type        = string
  default     = "e2-standard-4"
}

variable "system_node_count_max" {
  description = "Upper bound of the system node pool autoscaler."
  type        = number
  default     = 3
}

variable "gpu_machine_type" {
  description = "Machine type of the GPU node pool; G2 machine types carry NVIDIA L4 GPUs."
  type        = string
  default     = "g2-standard-8"
}

variable "gpu_node_count_max" {
  description = "Upper bound of the GPU node pool autoscaler. The pool scales from zero."
  type        = number
  default     = 1
}
