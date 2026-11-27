# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Check the native error linked by a hosted model diagnostic inside its synthetic worker."""

import json
import re
import sys
from pathlib import Path
from uuid import UUID


def model_denied(root: Path, conversation: str, model: str, sources: list[str]) -> bool:
    events = root / UUID(conversation).hex / "events"
    for source in sources:
        if str(UUID(source)) != source:
            return False
        for path in events.glob(f"event-*-{source}.json"):
            event = json.loads(path.read_bytes())
            if (event.get("id"), event.get("kind"), event.get("source"), event.get("code")) != (
                source,
                "ConversationErrorEvent",
                "environment",
                "APIError",
            ):
                continue
            detail = event.get("detail", "")
            if re.search(r"""["']type["']\s*:\s*["']key_model_access_denied["']""", detail) or (
                f"The requested model '{model}' is not available for this API key" in detail
            ):
                return True
    return False


if __name__ == "__main__":
    # Only a fixed receipt crosses the pod boundary after the check succeeds.
    try:
        config = json.loads(Path("/etc/heartwood/worker.json").read_bytes())
        denied = model_denied(
            Path(config["conversations_path"]), sys.argv[1], sys.argv[2], sys.argv[3:]
        )
    except (KeyError, OSError, TypeError, ValueError):
        denied = False
    if not denied:
        sys.exit(1)
    print("model-access-denial-verified")
