/*
 * This source file is part of the Heartwood open-source project
 *
 * SPDX-FileCopyrightText: 2026 Stanford University and the project authors (see CONTRIBUTORS.md)
 *
 * SPDX-License-Identifier: MIT
 */

import {
  HEARTWOOD_MARK_CORE,
  HEARTWOOD_MARK_RINGS,
  HEARTWOOD_MARK_VIEW_BOX,
} from "../brandMark.generated";

export const HeartwoodMark = ({ size = 18 }: { size?: number }) => (
  <svg
    aria-hidden="true"
    className="heartwood-mark"
    focusable="false"
    height={size}
    viewBox={HEARTWOOD_MARK_VIEW_BOX}
    width={size}
  >
    {HEARTWOOD_MARK_RINGS.map((ring) => (
      <path
        d={ring.d}
        fill="none"
        key={ring.d}
        stroke="currentColor"
        strokeOpacity={ring.opacity}
        strokeWidth={1.5}
      />
    ))}
    <path className="heartwood-mark-core" d={HEARTWOOD_MARK_CORE} />
  </svg>
);
