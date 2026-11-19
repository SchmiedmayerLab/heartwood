# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

# Zonal GKE Standard cluster for the reference platform: Dataplane V2 enforces
# NetworkPolicy, Workload Identity replaces node credentials for workloads, and a
# spot NVIDIA L4 node pool scales from zero for GPU jobs.

resource "google_project_service" "required" {
  for_each = toset(["compute.googleapis.com", "container.googleapis.com"])

  service = each.value
  # Removing this configuration leaves the APIs enabled for other users of the project.
  disable_on_destroy = false
}

resource "google_compute_network" "platform" {
  name                    = var.cluster_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.required]
}

resource "google_compute_subnetwork" "platform" {
  name                     = var.cluster_name
  region                   = var.region
  network                  = google_compute_network.platform.id
  ip_cidr_range            = "10.10.0.0/20"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/16"
  }

  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.30.0.0/20"
  }
}

# Nodes run as a dedicated service account that may only write logs and metrics.
# No service account key is created.
resource "google_service_account" "nodes" {
  account_id   = "${var.cluster_name}-nodes"
  display_name = "Nodes of ${var.cluster_name}"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])

  project = var.project_id
  role    = each.value
  member  = google_service_account.nodes.member
}

resource "google_container_cluster" "platform" {
  name     = var.cluster_name
  location = var.zone

  network         = google_compute_network.platform.id
  subnetwork      = google_compute_subnetwork.platform.id
  networking_mode = "VPC_NATIVE"

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  # Dataplane V2 enforces Kubernetes NetworkPolicy.
  datapath_provider = "ADVANCED_DATAPATH"

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  release_channel {
    channel = "REGULAR"
  }

  # Without initial_node_count and node_config the cluster has no default node pool;
  # every node belongs to the node pools below.

  # The documented removal procedure deletes the cluster with `tofu destroy`.
  deletion_protection = false
}

resource "google_container_node_pool" "system" {
  name     = "system"
  cluster  = google_container_cluster.platform.id
  location = var.zone

  autoscaling {
    min_node_count = 1
    max_node_count = var.system_node_count_max
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.system_machine_type
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }
}

resource "google_container_node_pool" "gpu" {
  name     = "gpu-l4-spot"
  cluster  = google_container_cluster.platform.id
  location = var.zone

  # The pool has no nodes until a pod requests a GPU.
  initial_node_count = 0

  autoscaling {
    min_node_count = 0
    max_node_count = var.gpu_node_count_max
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  node_config {
    machine_type    = var.gpu_machine_type
    spot            = true
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    # GKE installs the driver and taints the nodes with nvidia.com/gpu=present:NoSchedule.
    guest_accelerator {
      type  = "nvidia-l4"
      count = 1

      gpu_driver_installation_config {
        gpu_driver_version = "DEFAULT"
      }
    }

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }
  }
}
