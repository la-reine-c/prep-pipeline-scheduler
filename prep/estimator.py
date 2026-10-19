"""How long a prep takes.

Pure module, imported by both the scheduler and the web UI so they can never
disagree. Before any timings exist the pattern default applies:

    t = min(per_day * days, cap)

Once timings exist, a teacher's mean is shrunk towards the global mean:

    t_hat = (n * mean_teacher + k * mu) / (n + k)

so one freak result doesn't reset a teacher's profile.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import filtering
from .config import EstimatorConfig


@dataclass
class Estimate:
    minutes: int              # what to BOOK - a quantile, not the mean
    source: str               # pattern | global | shrunk | tracked | corrected
    detail: str
    central: int = 0          # the central estimate, for display
    spread: float = 0.0       # fractional spread, e.g. 0.19 = +/-19%

    def __post_init__(self) -> None:
        if not self.central:
            self.central = self.minutes

    def describe(self) -> str:
        return f"{self.minutes} min ({self.source}: {self.detail})"


def pattern_minutes(cfg: EstimatorConfig, assigned: datetime | None,
                    due: datetime) -> int:
    """The min(30d, 120) rule."""
    days = 1 if assigned is None else max((due.date() - assigned.date()).days, 1)
    return min(cfg.per_day_minutes * days, cfg.cap_minutes)


def confidence(teacher: str, teacher_stats: dict[str, tuple[int, float]],
               global_mean: float | None, overridden: bool = False,
               history: dict[str, list[float]] | None = None,
               min_tracked: int = 3) -> str:
    """The label for how well-founded an estimate is, computed live.

    The stored estimate_source is a snapshot from the last reschedule; after
    logging a few times it goes stale and would keep saying "guess only".
    """
    if overridden:
        return "corrected"
    if len((history or {}).get(teacher, [])) >= min_tracked:
        return "tracked"
    n, _ = teacher_stats.get(teacher, (0, 0.0))
    if n >= 5:
        return "measured"
    if n > 0:
        return "shrunk"
    return "global" if global_mean is not None else "pattern"


def estimate(cfg: EstimatorConfig, teacher: str, assigned: datetime | None,
             due: datetime, teacher_stats: dict[str, tuple[int, float]],
             global_mean: float | None,
             history: dict[str, list[float]] | None = None,
             filter_cfg: filtering.FilterConfig | None = None) -> Estimate:
    prior = pattern_minutes(cfg, assigned, due)
    n, mean = teacher_stats.get(teacher, (0, 0.0))

    # Enough of this teacher's own timings to track them properly.
    filter_cfg = filter_cfg or filtering.FilterConfig()
    timings = (history or {}).get(teacher, [])
    if len(timings) >= filter_cfg.min_observations:
        state = filtering.run(timings, filter_cfg, prior_minutes=global_mean)
        if state is not None:
            booked = int(round(min(state.session_minutes(filter_cfg),
                                   cfg.cap_minutes)))
            booked = max(booked, cfg.min_minutes)
            central = int(round(min(state.minutes, cfg.cap_minutes)))
            damped = f", {state.rejected} damped" if state.rejected else ""
            return Estimate(
                booked, "tracked",
                f"n={state.n}, centre {central} min, "
                f"+/-{state.relative_spread(filter_cfg)*100:.0f}%{damped}",
                central=central, spread=state.relative_spread(filter_cfg))

    if n == 0:
        # No timings for this teacher. Once ANY real timings exist, the average
        # of those beats the min(30d, 120) guess, which only ever encodes how
        # much time was available - not how much work was set. Without this, a
        # teacher you have not logged yet keeps the inflated estimate forever,
        # however much data you gather elsewhere.
        if global_mean is not None:
            blended = min(max(global_mean, cfg.min_minutes), prior)
            return Estimate(int(round(blended)), "global",
                            f"no timings for {teacher or 'unknown'}, "
                            f"mu={global_mean:.0f}")
        return Estimate(prior, "pattern", f"no timings for {teacher or 'unknown'}")

    # Shrink towards the global mean if we have one, otherwise towards the pattern.
    mu = global_mean if global_mean is not None else float(prior)
    k = cfg.shrinkage_k
    raw = (n * mean + k * mu) / (n + k)

    minutes = int(round(max(raw, cfg.min_minutes)))
    minutes = min(minutes, cfg.cap_minutes)
    source = "measured" if n >= 5 else "shrunk"
    return Estimate(minutes, source, f"n={n}, mean={mean:.0f}, mu={mu:.0f}")
