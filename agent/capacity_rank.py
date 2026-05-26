"""
agent/capacity_rank.py
----------------------
Phase 4a — capacity-weighted re-ranking.

Pure functions. Given:
  - an opportunity row (dict shaped like the /opportunities response)
  - the org's OrgCapacity row (active_pursuits_target + availability_windows)
  - the current count of Pursuing opportunities for the org

…produce a CapacityFit dataclass attached to the opportunity in
/opportunities responses:

    fit_label:         "open" / "tight" / "over" / "closed_window"
    score_adjustment:  signed float added to score_final for ranking
    warnings:          list of human-readable strings the UI shows
                       inline on the opportunity card

The score adjustment is intentionally small (max ±1.5) so capacity
nudges ranking but doesn't overwhelm the underlying mission-fit signal.

Used by portal/routers/opportunities.py and surfaced on the frontend's
Prospects view as inline warnings, and on the Pipeline view as the
"X of Y active pursuits" budget meter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime    import date
from typing      import Optional

from portal.models.capacity import is_within_window


# ─────────────────────────────────────────────────────────────────────────────
# Score-adjustment magnitudes
# ─────────────────────────────────────────────────────────────────────────────

# A deadline that falls inside a "closed window" — meaningful demotion so
# these opportunities don't sit at the top of the list during a freeze.
SCORE_ADJ_CLOSED_WINDOW   = -1.5

# Over capacity overall — demote new opportunities slightly so the user
# focuses on what they're already pursuing. Smaller than the window demote
# because over-capacity is a soft signal (you can always add one more).
SCORE_ADJ_OVER_CAPACITY   = -0.75

# At-target capacity — small nudge to be cautious.
SCORE_ADJ_AT_CAPACITY     = -0.25


@dataclass
class CapacityFit:
    fit_label:        str                                # 'open' | 'tight' | 'over' | 'closed_window'
    score_adjustment: float = 0.0
    warnings:         list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fit_label":        self.fit_label,
            "score_adjustment": round(self.score_adjustment, 3),
            "warnings":         list(self.warnings),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Per-opportunity re-rank
# ─────────────────────────────────────────────────────────────────────────────

def assess_opportunity(
    opportunity:         dict,
    capacity_target:     int,
    current_pursuing:    int,
    availability_windows:list[dict] | None,
) -> CapacityFit:
    """
    Score one opportunity against the org's capacity. Returns CapacityFit
    that the router merges into the JSON response.

    The opportunity dict is expected to look like a row from /opportunities
    (so it has application_deadline + pursuit.status). Missing fields are
    handled defensively.
    """
    warnings: list[str] = []
    adjustment = 0.0

    # ── Deadline-in-closed-window check ──────────────────────────────────────
    deadline_raw = opportunity.get("application_deadline")
    in_window, window_label = is_within_window(deadline_raw, availability_windows)
    if in_window:
        adjustment += SCORE_ADJ_CLOSED_WINDOW
        warnings.append(
            f"Deadline {deadline_raw} falls inside a closed window "
            f"({window_label}). You marked this period as unavailable for "
            f"new pursuits."
        )

    # ── Active-pursuits budget check ─────────────────────────────────────────
    # Skip the budget check entirely if THIS opportunity is already Pursuing —
    # we don't want to demote work the user has already committed to.
    pursuit = opportunity.get("pursuit") or {}
    is_already_pursuing = (pursuit.get("status") == "pursuing")

    if not is_already_pursuing:
        if current_pursuing > capacity_target:
            adjustment += SCORE_ADJ_OVER_CAPACITY
            warnings.append(
                f"You're over capacity: {current_pursuing} active pursuits "
                f"against your target of {capacity_target}. Consider closing "
                f"something out before opening this one."
            )
        elif current_pursuing == capacity_target:
            adjustment += SCORE_ADJ_AT_CAPACITY
            warnings.append(
                f"You're at your capacity target ({capacity_target} active "
                f"pursuits). Add this one only if you have a clear path to "
                f"reducing the others soon."
            )

    # ── Derive fit_label from the dominant signal ────────────────────────────
    if in_window:
        fit_label = "closed_window"
    elif not is_already_pursuing and current_pursuing > capacity_target:
        fit_label = "over"
    elif not is_already_pursuing and current_pursuing == capacity_target:
        fit_label = "tight"
    else:
        fit_label = "open"

    return CapacityFit(
        fit_label        = fit_label,
        score_adjustment = adjustment,
        warnings         = warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Capacity summary — used by the Pipeline meter
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CapacitySummary:
    active_pursuits_target: int
    current_pursuing:       int
    headroom:               int        # target - current (can be negative)
    utilization_pct:        float      # current / target (cap at 200%)
    closed_windows_active:  list[dict] # windows that contain today's date
    next_closed_window:     Optional[dict]


def summarize_capacity(
    capacity_target:     int,
    current_pursuing:    int,
    availability_windows:list[dict] | None,
    today:               date | None = None,
) -> CapacitySummary:
    """
    Lightweight aggregate for the Pipeline header. Pure; no DB.
    """
    today = today or date.today()
    headroom = capacity_target - current_pursuing
    util = (current_pursuing / capacity_target * 100.0) if capacity_target else 0.0
    util = min(util, 200.0)

    in_now, label = is_within_window(today, availability_windows)
    active_windows: list[dict] = []
    next_window:    Optional[dict] = None
    if availability_windows:
        for win in availability_windows:
            in_w, _ = is_within_window(today, [win])
            if in_w:
                active_windows.append(win)
            else:
                # Find the next future window by start date
                from portal.models.capacity import _coerce_date
                start = _coerce_date(win.get("start"))
                if start and start > today:
                    if (next_window is None
                        or _coerce_date(next_window.get("start")) > start):
                        next_window = win

    return CapacitySummary(
        active_pursuits_target = capacity_target,
        current_pursuing       = current_pursuing,
        headroom               = headroom,
        utilization_pct        = round(util, 1),
        closed_windows_active  = active_windows,
        next_closed_window     = next_window,
    )
