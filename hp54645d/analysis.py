# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Measurements on captured traces, over the whole record or a time window.

The same quantities the scope's own measure menu offers, computed here from
the data, so they work on anything --- a fresh readout, a million-point
memory record, or a file saved last week --- and on any stretch of it::

    from hp54645d.analysis import measure
    measure(capture)                                  # every channel, whole record
    measure(capture, start=-1e-6, stop=5e-6)          # just a window

Frequency, period and duty cycle come from crossings of the middle of the
signal's swing, with hysteresis (40 % and 60 % of the swing) so noise on an
edge is not counted as extra edges. They need at least two rising edges in the
window, and with more, regular ones --- gaps varying by less than 25 % --- so
that a flat stretch of noise, whose "swing" is the noise itself, is not
reported as a frequency. Otherwise they are left out rather than guessed.

Vectorised throughout: a million points take a few milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from hp54645d.waveform import AnalogTrace, Capture, DigitalTrace


@dataclass(frozen=True)
class Measurement:
    """One measured quantity."""

    name: str
    value: float
    unit: str


def measure(
    capture: Capture, *, start: float | None = None, stop: float | None = None
) -> dict[str, list[Measurement]]:
    """Measure every trace in a capture.

    Args:
        capture: What to measure.
        start: Beginning of the window, in seconds from the trigger.
            Default: the start of each record.
        stop: End of the window. Default: the end of each record.

    Returns:
        Trace name (``"ch1"``, ``"D0"``) to its measurements, in display
        order. A trace with no samples in the window is left out.
    """
    results: dict[str, list[Measurement]] = {}
    for n, trace in capture.analog.items():
        found = measure_analog(trace, start=start, stop=stop)
        if found:
            results[f"ch{n}"] = found
    for n, trace in capture.digital.items():
        found = measure_digital(trace, start=start, stop=stop)
        if found:
            results[f"D{n}"] = found
    return results


def measure_analog(
    trace: AnalogTrace, *, start: float | None = None, stop: float | None = None
) -> list[Measurement]:
    """Levels, and the timing of a repetitive signal, for one analog trace."""
    times, volts = _window(trace.time_s, trace.volts, start, stop)
    if len(volts) == 0:
        return []
    low, high = float(volts.min()), float(volts.max())
    found = [
        Measurement("min", low, "V"),
        Measurement("max", high, "V"),
        Measurement("pk-pk", high - low, "V"),
        Measurement("mean", float(volts.mean()), "V"),
        Measurement("rms", float(np.sqrt(np.mean(np.square(volts)))), "V"),
    ]
    if high > low:
        is_high = _hysteresis(volts, low + 0.4 * (high - low), low + 0.6 * (high - low))
        found += _timing(times, is_high)
    return found


def measure_digital(
    trace: DigitalTrace, *, start: float | None = None, stop: float | None = None
) -> list[Measurement]:
    """How much of the time a logic channel is high, and its timing."""
    times, levels = _window(trace.time_s, trace.levels, start, stop)
    if len(levels) == 0:
        return []
    return [Measurement("high", 100 * float(np.mean(levels)), "%"), *_timing(times, levels)]


# -- internals ------------------------------------------------------------------


def _window(times: np.ndarray, values: np.ndarray, start: float | None, stop: float | None):
    first = 0 if start is None else int(np.searchsorted(times, start, side="left"))
    last = len(times) if stop is None else int(np.searchsorted(times, stop, side="right"))
    return times[first:last], values[first:last]


def _hysteresis(values: np.ndarray, below: float, above: float) -> np.ndarray:
    """High/low state, switching only on crossing ``above`` or ``below``.

    Samples between the two thresholds keep the state of the last one outside
    them; before the first such sample, the state is taken from the midpoint.
    """
    definite = (values > above) | (values < below)
    index = np.where(definite, np.arange(len(values)), 0)
    np.maximum.accumulate(index, out=index)
    state = values[index] > (above + below) / 2
    first = int(np.argmax(definite)) if definite.any() else len(values)
    state[:first] = values[:first] > (above + below) / 2
    return state


#: How much the gaps between rising edges may vary (standard deviation over
#: mean) and still count as one repeating signal.
_REGULARITY = 0.25


def _timing(times: np.ndarray, is_high: np.ndarray) -> list[Measurement]:
    """Frequency, period and duty cycle from a high/low state.

    Needs two rising edges, and with three or more, regular ones. The second
    condition is what keeps noise from being measured: on a stretch with no
    real edges the swing is just noise, the thresholds fall inside it, and it
    crosses them at random intervals --- nothing like a period.
    """
    is_high = np.asarray(is_high, dtype=bool)
    rising = np.flatnonzero(~is_high[:-1] & is_high[1:]) + 1
    if len(rising) < 2:
        return []
    gaps = np.diff(times[rising])
    if len(gaps) >= 2 and np.std(gaps) > _REGULARITY * np.mean(gaps):
        return []
    first, last = rising[0], rising[-1]
    period = float(times[last] - times[first]) / (len(rising) - 1)
    duty = 100 * float(np.mean(is_high[first:last]))
    return [
        Measurement("frequency", 1 / period, "Hz"),
        Measurement("period", period, "s"),
        Measurement("duty", duty, "%"),
    ]
