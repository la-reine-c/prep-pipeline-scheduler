"""Tracking how long a teacher's prep really takes, as it drifts.

A flat per-teacher mean treats a September timing and a June one as equally
current, which is wrong: topics get harder through the year and you get faster.
This tracks a slowly-varying state instead.

The model is a scalar random walk in log space:

    x_k = x_{k-1} + w,    w ~ N(0, Q)        the true log-duration drifts
    z_k = x_k + v,        v ~ N(0, R)        your logged time, noisily

Log space because durations are positive and right-skewed: it keeps the
estimate positive and makes the noise roughly symmetric.

Two deliberate departures from a textbook Kalman filter:

1. **Huber-weighted updates.** A plain Kalman update is linear in the
   innovation, so one 300-minute evening because you got distracted drags the
   state proportionally hard. Down-weighting large innovations resists that,
   and unlike hard gating it never discards a reading outright - a genuinely
   long prep still moves the estimate, just not all at once.

2. **A quantile, not the mean, is what gets scheduled.** Booking the mean means
   running out of time on half your preps by construction. See `session_minutes`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class FilterConfig:
    process_noise: float = 0.010     # Q: how fast the true duration drifts
    measurement_noise: float = 0.090  # R: log-space spread of your own timings
    huber_c: float = 1.5             # innovations beyond this many sigma are damped
    initial_variance: float = 0.25   # P0: wide, so the first reading moves it a lot
    min_observations: int = 3        # below this, keep the shrinkage estimate
    quantile_lambda: float = 0.85    # ~80th percentile of the predictive lognormal
    max_weight_inflation: float = 25.0
    adapt_after: int = 4             # observations needed before R is re-estimated
    min_measurement_noise: float = 0.02
    max_measurement_noise: float = 0.50


@dataclass
class FilterState:
    mean_log: float          # x: filtered log-minutes
    var_log: float           # P: variance of that estimate
    n: int
    rejected: int = 0        # readings the Huber weight pushed below half
    measurement_noise: float = 0.09   # the R this state was filtered with

    @property
    def minutes(self) -> float:
        """The central estimate, back in minutes."""
        return math.exp(self.mean_log)

    def predictive_sd(self, cfg: FilterConfig) -> float:
        """Spread of the NEXT reading, not of the estimate.

        P alone is how well the mean is pinned down; a new prep also carries
        the measurement spread R, and scheduling has to cover both.
        """
        return math.sqrt(max(self.var_log, 0.0) + self.measurement_noise)

    def session_minutes(self, cfg: FilterConfig) -> float:
        """How long to actually book.

            t = exp(x + lambda * sqrt(P + R))

        Booking exp(x) - the median - means overrunning half the time. Shifting
        up by lambda sigma buys a finish rate of roughly Phi(lambda): about 80%
        at the default.
        """
        return math.exp(self.mean_log + cfg.quantile_lambda * self.predictive_sd(cfg))

    def relative_spread(self, cfg: FilterConfig) -> float:
        """A unitless "how sure are we", for the dashboard.

        exp(sd) - 1 is the fractional spread: 0.3 means roughly +/-30%.
        """
        return math.exp(self.predictive_sd(cfg)) - 1.0


def huber_weight(standardised: float, c: float) -> float:
    """1 inside the bend, falling off as c/|s| outside it.

    This is the Huber psi-function's weight. Bounded influence: a wild reading
    contributes about as much as one sitting exactly on the bend, never more.
    """
    magnitude = abs(standardised)
    if magnitude <= c or magnitude == 0.0:
        return 1.0
    return c / magnitude


def _robust_scale(values: list[float]) -> float:
    """Median absolute deviation, scaled to be a standard-deviation estimate.

    Used instead of the sample sd because the whole point is to be unmoved by
    the one evening that went wrong - and that evening is in this list.
    """
    if len(values) < 2:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2
              else (ordered[middle - 1] + ordered[middle]) / 2)
    deviations = sorted(abs(value - median) for value in values)
    mid = len(deviations) // 2
    mad = (deviations[mid] if len(deviations) % 2
           else (deviations[mid - 1] + deviations[mid]) / 2)
    return 1.4826 * mad


def _adapt_measurement_noise(observations: list[float], cfg: FilterConfig,
                             prior_minutes: float | None) -> float:
    """Re-estimate R from how much this person's own timings actually vary.

    A fixed R mis-calibrates the booked time for anyone whose spread differs
    from the default: too generous if they are consistent, too tight if not.
    R is taken from the innovations of a first pass, robustly.
    """
    if len(observations) < cfg.adapt_after:
        return cfg.measurement_noise

    # Deliberately the spread of the raw log timings, NOT of the filter's
    # residuals. Residuals from a filter that has already chased the data are
    # far too small, which under-books the time. Raw spread also folds in drift
    # - and for booking purposes that is right, because whatever makes a prep
    # run long still makes it run long.
    scale = _robust_scale([math.log(value) for value in observations])
    if scale <= 0:
        return cfg.measurement_noise

    # MAD is biased low on few samples, and under-booking is the failure that
    # actually costs you an evening. The 1 + 1.4/n correction pulls small-sample
    # estimates back up; it fades to nothing as timings accumulate.
    scale *= 1.0 + 1.4 / len(observations)
    variance = scale ** 2
    return min(max(variance, cfg.min_measurement_noise), cfg.max_measurement_noise)


def _filter_once(observations: list[float], cfg: FilterConfig,
                 prior_minutes: float | None, measurement_noise: float,
                 collect_innovations: bool = False):
    if prior_minutes and prior_minutes > 0:
        mean_log = math.log(prior_minutes)
    else:
        mean_log = math.log(observations[0])
    var_log = cfg.initial_variance

    innovations: list[float] = []
    rejected = 0
    for value in observations:
        var_pred = var_log + cfg.process_noise
        innovation = math.log(value) - mean_log
        spread = math.sqrt(var_pred + measurement_noise)
        standardised = innovation / spread if spread > 0 else 0.0
        if collect_innovations:
            innovations.append(innovation)

        weight = huber_weight(standardised, cfg.huber_c)
        if weight < 0.5:
            rejected += 1
        inflation = min(1.0 / max(weight, 1e-6), cfg.max_weight_inflation)
        effective_r = measurement_noise * inflation

        gain = var_pred / (var_pred + effective_r)
        mean_log += gain * innovation
        var_log = (1.0 - gain) * var_pred

    return (FilterState(mean_log=mean_log, var_log=var_log,
                        n=len(observations), rejected=rejected,
                        measurement_noise=measurement_noise), innovations)


def run(observations: list[float], cfg: FilterConfig | None = None,
        prior_minutes: float | None = None) -> FilterState | None:
    """Filter a teacher's timings, oldest first. None if there is nothing to go on.

    `prior_minutes` seeds the state - pass the global mean so a teacher's first
    reading is a correction to something sensible rather than to nothing.
    """
    cfg = cfg or FilterConfig()
    usable = [float(value) for value in observations if value and value > 0]
    if not usable:
        return None

    measurement_noise = _adapt_measurement_noise(usable, cfg, prior_minutes)
    state, _ = _filter_once(usable, cfg, prior_minutes, measurement_noise)
    return state


def steady_state_gain(cfg: FilterConfig) -> float:
    """What the gain converges to, for sanity-checking a tuning.

        P- = (Q + sqrt(Q^2 + 4QR)) / 2,   K = P- / (P- + R)

    A constant gain is an EWMA, so this is the effective smoothing: K = 0.3
    means each new reading moves the estimate about 30% of the way.
    """
    q, r = cfg.process_noise, cfg.measurement_noise
    var_pred = (q + math.sqrt(q * q + 4 * q * r)) / 2
    return var_pred / (var_pred + r)
