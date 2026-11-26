# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

output "cluster_name" {
  description = "Name of the cluster."
  value       = google_container_cluster.platform.name
}

output "cluster_location" {
  description = "Zone of the cluster."
  value       = google_container_cluster.platform.location
}

output "workload_pool" {
  description = "Workload Identity pool of the cluster."
  value       = google_container_cluster.platform.workload_identity_config[0].workload_pool
}

output "gpu_node_pool" {
  description = "Name of the spot NVIDIA L4 node pool."
  value       = google_container_node_pool.gpu.name
}
