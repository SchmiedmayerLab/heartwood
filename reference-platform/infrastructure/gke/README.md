<!--

This source file is part of the Heartwood open-source project

SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)

SPDX-License-Identifier: MIT

-->

# GKE Infrastructure

This OpenTofu configuration defines the Google Kubernetes Engine (GKE) cluster for the reference platform.
It holds every cloud-specific setting of that deployment.

This configuration has not been applied to a Google Cloud project.
Continuous integration checks it offline with `tofu fmt -check` and `tofu validate`, which contact no cloud provider.

## Resources

| Resource | Configuration |
|---|---|
| Cluster | Zonal GKE Standard cluster, `REGULAR` release channel, control plane version 1.36.4-gke.1469000 or later, no default node pool |
| Network | Dedicated VPC network and subnet with secondary ranges for pods and services |
| Network policy | Dataplane V2 (`ADVANCED_DATAPATH`), which enforces Kubernetes NetworkPolicy |
| Workload identity | Workload Identity pool `PROJECT.svc.id.goog`; node pools expose the GKE metadata server to pods |
| System node pool | `e2-standard-4` nodes, autoscaled from one to three |
| GPU node pool | `g2-standard-8` spot nodes with one NVIDIA L4 GPU each, autoscaled from zero to one; GKE installs the driver; the pool taints its nodes with `nvidia.com/gpu=present:NoSchedule` |
| Node identity | Dedicated service account with the role `roles/container.defaultNodeServiceAccount` only |
| Access | Nodes have public addresses; the control plane endpoint accepts authenticated requests from any address |
| APIs | Compute Engine and Kubernetes Engine, left enabled on removal |

The configuration creates no service account keys.
Variables in `variables.tf` set the project, region, zone, cluster name, machine types and autoscaler bounds.
The chart values for this cluster are in `../../chart/heartwood-platform/values-gke.yaml`.

## Prerequisites

- OpenTofu 1.13 or later.
- A Google Cloud project with billing and quota for one spot NVIDIA L4 GPU in the chosen region.
- An operator account that can enable APIs and manage GKE clusters, VPC networks, service accounts and project IAM bindings.
- Application default credentials from the operator's own sign-in: `gcloud auth application-default login`.

## Apply

From the repository root:

```bash
tofu -chdir=reference-platform/infrastructure/gke init && tofu -chdir=reference-platform/infrastructure/gke apply -var project_id=PROJECT
```

A repeated apply of an unchanged configuration is expected to report no changes; this has not been verified on a cluster.
OpenTofu keeps the state in `terraform.tfstate` in this directory.
Git ignores that file; keep it, because removal needs it.

To use the cluster with `kubectl`:

```bash
gcloud container clusters get-credentials heartwood-reference --zone us-central1-a --project PROJECT
```

## Remove

Deleting a cluster leaves the Persistent Disks of its dynamically provisioned volumes in the project.
Load balancers that Services of type `LoadBalancer`, Ingress or Gateway resources created also stay, and they keep the network from being deleted.
Uninstall the chart, delete every PersistentVolumeClaim, wait until every PersistentVolume is gone, then run:

```bash
tofu -chdir=reference-platform/infrastructure/gke destroy -var project_id=PROJECT
```

Removal deletes the cluster, its node pools, the network, the node service account and its IAM bindings.
The Compute Engine and Kubernetes Engine APIs stay enabled, because other resources in the project can depend on them.
