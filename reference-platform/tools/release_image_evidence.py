# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Verify and retain the three successful cluster jobs' published-image observations."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from publish_images import read_record
from qualification_images import verify

WORKFLOW = ".github/workflows/native-qualification.yml"
STAGES = ("platform", "hosted", "worker")


def main() -> None:
    """Check the selected qualification run and retain its verified records."""
    repository = os.environ["GITHUB_REPOSITORY"]
    commit = os.environ["GITHUB_SHA"]
    run = os.environ["QUALIFICATION_RUN"]
    api = f"repos/{repository}/actions"
    directory = Path("test-results/release-evidence/images")
    directory.mkdir(parents=True, exist_ok=True)

    def gh(*args: str) -> bytes:
        return subprocess.check_output(["gh", *args])

    def signed(name: str, artifact: str) -> Path:
        with TemporaryDirectory() as scratch:
            gh("run", "download", run, "--repo", repository, "--name", artifact, "--dir", scratch)
            path = directory / name
            path.write_bytes((Path(scratch) / name).read_bytes())
        gh(
            "attestation",
            "verify",
            str(path),
            "--repo",
            repository,
            "--signer-workflow",
            f"{repository}/{WORKFLOW}",
            "--source-ref",
            "refs/heads/main",
            "--source-digest",
            commit,
        )
        return path

    metadata = json.loads(gh("api", f"{api}/runs/{run}"))
    if (
        str(metadata["id"]) != run
        or metadata["path"] != WORKFLOW
        or metadata["head_branch"] != "main"
        or metadata["head_sha"] != commit
        or metadata["head_repository"]["full_name"] != repository
        or metadata["event"] not in {"push", "workflow_dispatch"}
        or metadata["conclusion"] != "success"
    ):
        raise ValueError("expected a successful native qualification for this main commit")
    attempt = str(metadata["run_attempt"])
    receipt = json.loads(
        signed("stage-receipt.json", f"stage-receipt-{attempt}-{commit}").read_bytes()
    )
    ids = receipt["stages"]
    if (
        receipt != {"commit": commit, "run": run, "attempt": attempt, "stages": ids}
        or set(ids) != set(STAGES)
        or len(set(ids.values())) != len(STAGES)
        or not all(re.fullmatch(r"[1-9][0-9]*", value) for value in ids.values())
    ):
        raise ValueError("incorrect stage receipt")
    publication = signed("published-images.json", f"published-images-{commit}")
    drill_publication = directory.parent / "published" / publication.name
    if drill_publication.exists() and drill_publication.read_bytes() != publication.read_bytes():
        raise ValueError("route drills and cluster stages reference different publication records")
    record = read_record(publication)
    pages = json.loads(
        gh("api", "--paginate", "--slurp", f"{api}/runs/{run}/attempts/{attempt}/jobs")
    )
    jobs = [job for page in pages for job in page["jobs"]]
    for stage in STAGES:
        name = f"Kubernetes Qualification / Kubernetes qualification of the {stage} stage"
        producers = [job for job in jobs if job["name"] == name]
        if len(producers) != 1:
            raise ValueError(f"expected one {stage} producer")
        job = producers[0]
        if (
            job["conclusion"] != "success"
            or job["head_sha"] != commit
            or str(job["run_id"]) != run
            or str(job["run_attempt"]) != attempt
        ):
            raise ValueError(f"unsuccessful or mismatched {stage} producer")
        artifact = f"{api}/artifacts/{ids[stage]}"
        info = json.loads(gh("api", artifact))
        if (
            str(info["id"]) != ids[stage]
            or info["expired"]
            or info["name"] != f"observed-platform-images-{stage}-{attempt}-{commit}"
            or str(info["workflow_run"]["id"]) != run
            or info["workflow_run"]["head_sha"] != commit
        ):
            raise ValueError(f"incorrect {stage} artifact")
        downloaded = gh("api", artifact + "/zip")
        if info["digest"] != "sha256:" + hashlib.sha256(downloaded).hexdigest():
            raise ValueError(f"incorrect {stage} artifact digest")
        with zipfile.ZipFile(io.BytesIO(downloaded)) as archive:
            if archive.namelist() != ["observed-images.json"]:
                raise ValueError(f"incorrect {stage} artifact contents")
            raw = archive.read("observed-images.json")
        verify(record, json.loads(raw), commit, stage, run, attempt)
        (directory / f"{stage}.json").write_bytes(raw)


if __name__ == "__main__":
    main()
