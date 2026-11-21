# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT
"""Emit only the project list override for the dedicated hosted browser fixture."""

import json
import sys
from pathlib import Path

import yaml

values = yaml.safe_load(Path(sys.argv[1]).read_text())
projects = {item["id"]: item for item in values["projects"]}
subjects = {item["username"]: item["subject"] for item in values["researchers"]}
first, second = subjects["researcher-one"], subjects["researcher-two"]
assert set(projects["demo"]["members"]) == {first, second}
assert projects["other"]["members"] == projects["small"]["members"] == [first]
projects["other"]["members"].append(second)
print(json.dumps({"projects": values["projects"]}))
