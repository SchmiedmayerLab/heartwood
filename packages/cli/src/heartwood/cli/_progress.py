# This source file is part of the Heartwood open-source project
#
# SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
#
# SPDX-License-Identifier: MIT

"""Line-safe progress reporting for blocking command-line operations."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

from heartwood.cli._brand import progress_frames, progress_line
from heartwood.cli._interactive import InteractionActivity


def _format_transfer_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024**2:
        return f"{value / 1024:.1f} KiB"
    if value < 1024**3:
        return f"{value / 1024**2:.1f} MiB"
    return f"{value / 1024**3:.2f} GiB"


def _run_with_progress[Result](
    operation: Callable[[], Result],
    *,
    activity: InteractionActivity,
    update_interval: float = 15,
) -> Result:
    """Run one blocking operation with animated TTY or line-safe status updates."""
    stopped = threading.Event()
    started = time.monotonic()
    animated = sys.stderr.isatty() and "NO_COLOR" not in os.environ
    frames = progress_frames(sys.stderr)
    frame = 0

    def report_progress() -> None:
        nonlocal frame
        interval = min(update_interval, 0.4) if animated else update_interval
        while not stopped.wait(max(interval, 0.01)):
            elapsed = int(time.monotonic() - started)
            if animated:
                label = activity.label if elapsed < 10 else activity.waiting_label
                suffix = "" if elapsed < 10 else f" ({elapsed}s elapsed)"
                marker = frames[frame % len(frames)]
                frame += 1
                print(
                    f"\r\033[2K{progress_line(label, marker)}{suffix}",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"{activity.waiting_label} ({elapsed}s elapsed). {activity.guidance}",
                    file=sys.stderr,
                    flush=True,
                )

    if animated:
        print(progress_line(activity.label, frames[0]), end="", file=sys.stderr, flush=True)
    else:
        print(f"{activity.label}...", file=sys.stderr, flush=True)
    reporter = threading.Thread(
        target=report_progress,
        name="heartwood-line-progress",
        daemon=True,
    )
    reporter.start()
    try:
        return operation()
    finally:
        stopped.set()
        reporter.join()
        if animated:
            print("\r\033[2K", end="", file=sys.stderr, flush=True)
