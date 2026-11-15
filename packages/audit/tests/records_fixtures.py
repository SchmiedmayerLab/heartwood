# This source file is part of the Heartwood open-source project
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
# SPDX-License-Identifier: MIT

"""Fault fixtures shared by audit authority and session-boundary tests."""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast


@dataclass
class StoredObject:
    content: bytes
    deadline: datetime
    version: str
    mode: str = "COMPLIANCE"


@dataclass
class MemoryS3:
    """SDK fault fixture only; it supplies no evidence of real storage enforcement."""

    objects: dict[str, list[StoredObject]] = field(default_factory=dict)
    delete_markers: list[str] = field(default_factory=list)
    lost_response: bool = False
    fail_before_write: bool = False
    writes: int = 0
    calls: int = 0
    unavailable: bool = False

    def list_object_versions(self, **kwargs: object) -> Mapping[str, Any]:
        self.calls += 1
        if self.unavailable:
            raise OSError("synthetic records outage")
        prefix = str(kwargs["Prefix"])
        return {
            "Versions": [
                {"Key": key, "VersionId": value.version, "IsLatest": index == len(values) - 1}
                for key, values in self.objects.items()
                if key.startswith(prefix)
                for index, value in enumerate(values)
            ],
            "DeleteMarkers": [key for key in self.delete_markers if key.startswith(prefix)],
            "IsTruncated": False,
        }

    def get_object(self, **kwargs: object) -> Mapping[str, Any]:
        self.calls += 1
        if self.unavailable:
            raise OSError("synthetic records outage")
        value = next(
            item for item in self.objects[str(kwargs["Key"])] if item.version == kwargs["VersionId"]
        )
        return {
            "Body": io.BytesIO(value.content),
            "ContentLength": len(value.content),
            "VersionId": value.version,
            "ObjectLockMode": value.mode,
            "ObjectLockRetainUntilDate": value.deadline,
        }

    def put_object(self, **kwargs: object) -> Mapping[str, Any]:
        self.calls += 1
        if self.unavailable:
            raise OSError("synthetic records outage")
        if self.fail_before_write:
            raise OSError("synthetic transport failure")
        assert kwargs["IfNoneMatch"] == "*"
        assert kwargs["ObjectLockMode"] == "COMPLIANCE"
        assert kwargs["ChecksumAlgorithm"] == "SHA256"
        key = str(kwargs["Key"])
        if key in self.objects:
            raise ValueError("synthetic precondition failure")
        self.writes += 1
        self.objects[key] = [
            StoredObject(
                cast(bytes, kwargs["Body"]),
                cast(datetime, kwargs["ObjectLockRetainUntilDate"]),
                str(self.writes),
            )
        ]
        if self.lost_response:
            raise OSError("synthetic response loss after server acceptance")
        return {"VersionId": str(self.writes)}
