# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Run each declared managed profile with its own bounded native qualification."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from gke_qualification import read, write
from release_drills import ROUTES, require_managed_cleanup, supported_routes
from worker_model_profile import QualificationModelProfile


def main() -> None:
    """Qualify each protected profile against the exact published images."""
    routes = supported_routes(ROUTES)
    profiles = json.loads(os.environ["ROUTE_PROFILES"])
    credentials = json.loads(os.environ.pop("ROUTE_CREDENTIALS"))
    if set(routes) != profiles.keys() or set(routes) != credentials.keys():
        raise ValueError("protected route configuration must match the declared inventory")
    root = Path(os.environ["RUN_ROOT"])
    published = read(root / "published-images.json")
    args = []
    for name in ("worker", "runtime"):
        image = published["images"][name]
        args.extend(["--" + name + "-image", image["repository"] + "@" + image["digest"]])
    for index, route in enumerate(routes, 1):
        profile = root / f"profile-{index}.json"
        checked = QualificationModelProfile(**profiles[route])
        if checked.model + "@" + str(urlsplit(checked.base_url).hostname) != route:
            raise ValueError("profile and credential route differ")
        write(profile, checked.mapping())
        output = root / "observations" / f"route-{index}"
        completed = subprocess.run(
            [
                sys.executable,
                "deploy/qualify-native-worker.py",
                *args,
                "--gateway-artifacts",
                str(root / "gateway"),
                "--records-artifacts",
                str(root / "records"),
                "--model-profile",
                str(profile),
                "--output",
                str(output),
            ],
            input=credentials.pop(route).encode("ascii") + b"\n",
            check=False,
        )
        # An executed negative scenario is reportable. Assembly checks the complete
        # native record and both bounded observations before any upload.
        if completed.returncode and not (output / "SHA256SUMS").is_file():
            raise RuntimeError("managed qualification stopped before writing evidence")
        require_managed_cleanup(output)


if __name__ == "__main__":
    main()
