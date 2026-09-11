# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Draw a capture the way the scope shows it.

Two layouts for the analog channels:

``"split"``
    One panel per channel, each in volts over its own screen span. Every
    channel keeps an honest volt axis, whatever the scales.

``"overlay"``
    One screen, as on the instrument. Each channel is placed by its own
    V/div and offset on the shared 8-division graticule, so when the scales
    differ the axis is in **divisions** and the legend carries each channel's
    scale; a numbered marker on the left edge shows where each channel's 0 V
    sits, like the ground markers on the scope. When all channels share a
    scale, the axis stays in volts.

Logic channels always get their own panel below.

A full-memory record can be a million points or more. Analog traces are drawn
as a min/max envelope of at most a few thousand bins --- so a one-sample spike
still shows --- and re-thinned whenever the visible time range changes, so
zooming in with the toolbar reveals the real samples. Logic traces are drawn
from their transitions only, which is exact at any length.

Works on any matplotlib ``Figure``, so the GUI draws into its own canvas and a
script gets a figure it can ``show()`` or ``savefig()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from matplotlib.ticker import MaxNLocator, MultipleLocator

from hp54645d.settings import HORIZONTAL_DIVISIONS, VERTICAL_DIVISIONS
from hp54645d.waveform import Capture

if TYPE_CHECKING:
    from matplotlib.figure import Figure

SCREEN = "#101418"
GRID = "#39414a"
INK = "#e6e8ea"
MUTED = "#9aa3ab"
TRACE = {1: "#f2c94c", 2: "#56c7e0"}
LOGIC = "#7bd88f"

LAYOUTS = ("split", "overlay")

#: Above this many samples in view, an analog trace is drawn as a min/max
#: envelope of this many bins.
ENVELOPE_BINS = 3000

_UNITS = ((1.0, "s"), (1e-3, "ms"), (1e-6, "µs"), (1e-9, "ns"))
_HALF = VERTICAL_DIVISIONS / 2


def plot_capture(capture: Capture, figure: Figure | None = None, *,
                 layout: str = "split") -> Figure:
    """Draw a capture as a scope screen.

    Args:
        capture: What :meth:`~hp54645d.HP54645D.read_screen` or
            :meth:`~hp54645d.HP54645D.acquire` returned.
        figure: A figure to draw into; it is cleared first. By default a new
            pyplot figure, so ``plt.show()`` displays it.
        layout: ``"split"`` for a panel per analog channel, ``"overlay"`` for
            all of them on one screen.

    Returns:
        The figure.

    Raises:
        ValueError: For an unknown layout.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; use one of {LAYOUTS}")
    if figure is None:
        import matplotlib.pyplot as plt

        figure = plt.figure(figsize=(10, 6.5))
    figure.clear()
    figure.set_facecolor(SCREEN)

    channels = list(capture.analog)
    if layout == "overlay" and len(channels) > 1:
        panels = [("overlay", tuple(channels))]
    else:
        panels = [("analog", n) for n in channels]
    if capture.digital:
        panels.append(("digital", None))
    if not panels:
        ax = figure.add_subplot()
        _style(ax)
        ax.text(0.5, 0.5, "nothing captured", color=MUTED, ha="center", transform=ax.transAxes)
        return figure

    heights = [max(1.5, 0.35 * len(capture.digital)) if kind == "digital"
               else 4.0 if kind == "overlay" else 3.0 for kind, _ in panels]
    grid = figure.add_gridspec(len(panels), 1, height_ratios=heights, hspace=0.12)

    start, stop = _time_span(capture)
    scale, unit = time_axis(capture)
    per_div = capture.settings.timebase.seconds_per_div
    divisions = (stop - start) / per_div if per_div > 0 else 0

    axes = []
    for row, (kind, which) in enumerate(panels):
        ax = figure.add_subplot(grid[row], sharex=axes[0] if axes else None)
        axes.append(ax)
        _style(ax)
        if 4 <= divisions <= 60:
            ax.xaxis.set_major_locator(_DivisionLocator(per_div / scale))
        else:
            ax.set_xticks(np.linspace(start, stop, HORIZONTAL_DIVISIONS + 1) / scale)
        ax.set_xlim(start / scale, stop / scale)
        if start <= 0.0 <= stop:
            ax.axvline(0.0, color=MUTED, linewidth=0.8, linestyle="--")  # the trigger
        if kind == "analog":
            _draw_analog(ax, capture, which, scale)
        elif kind == "overlay":
            _draw_overlay(ax, capture, which, scale)
        else:
            _draw_digital(ax, capture, scale)
        if row < len(panels) - 1:
            ax.tick_params(labelbottom=False)

    axes[-1].set_xlabel(f"time from trigger ({unit})", color=MUTED)
    settings = capture.settings
    trigger = settings.trigger
    source = f"{capture.file.name} ({capture.origin})" if capture.file else capture.origin
    figure.suptitle(
        f"HP 54645D  ·  {source}  ·  {capture.points:,} points  ·  "
        f"{capture.timestamp:%Y-%m-%d %H:%M:%S}     "
        f"{_si(settings.timebase.seconds_per_div, 's')}/div     "
        f"trigger {trigger.source.lower()} {trigger.slope.lower()} {trigger.level_v:g} V "
        f"({trigger.mode.lower()})",
        color=INK, fontsize=10, x=0.01, ha="left",
    )
    figure.subplots_adjust(left=0.08, right=0.98, top=0.93, bottom=0.08)
    return figure


def _draw_analog(ax, capture: Capture, channel: int, scale: float) -> None:
    """One channel in its own panel, in volts."""
    trace = capture.analog[channel]
    setup = capture.settings.analog[channel]
    bottom, top = setup.offset_v - setup.range_v / 2, setup.offset_v + setup.range_v / 2
    ax.set_ylim(bottom, top)
    ax.set_yticks(np.linspace(bottom, top, VERTICAL_DIVISIONS + 1))
    _trace(ax, trace.time_s / scale, trace.volts, color=TRACE.get(channel, INK))

    trigger = capture.settings.trigger
    if trigger.source == f"ANALOG{channel}":
        ax.axhline(trigger.level_v, color=MUTED, linewidth=0.8, linestyle="--")
    ax.set_ylabel(f"ch{channel} (V)", color=MUTED)
    ax.text(0.005, 0.97, _describe(capture, channel), transform=ax.transAxes,
            color=INK, fontsize=9, va="top")
    ax.text(0.995, 0.97, f"min {trace.volts.min():.3g} V   max {trace.volts.max():.3g} V",
            transform=ax.transAxes, color=INK, fontsize=9, va="top", ha="right")


def _draw_overlay(ax, capture: Capture, channels: tuple[int, ...], scale: float) -> None:
    """Several channels on one screen, each at its own V/div and offset."""
    setups = [capture.settings.analog[n] for n in channels]
    shared = all(s.range_v == setups[0].range_v and s.offset_v == setups[0].offset_v
                 for s in setups)
    trigger = capture.settings.trigger

    if shared:
        # Same scale everywhere: the axis can stay in volts.
        reference = setups[0]
        bottom = reference.offset_v - reference.range_v / 2
        top = reference.offset_v + reference.range_v / 2
        ax.set_ylim(bottom, top)
        ax.set_yticks(np.linspace(bottom, top, VERTICAL_DIVISIONS + 1))
        ax.set_ylabel("volts", color=MUTED)

        def place(n, volts):
            return volts
    else:
        ax.set_ylim(-_HALF, _HALF)
        ax.set_yticks(np.arange(-_HALF, _HALF + 1))
        ax.set_ylabel("divisions", color=MUTED)

        def place(n, volts):
            setup = capture.settings.analog[n]
            return (np.asarray(volts) - setup.offset_v) / setup.volts_per_div

    for n in channels:
        trace = capture.analog[n]
        color = TRACE.get(n, INK)
        _trace(ax, trace.time_s / scale, place(n, trace.volts), color=color,
               label=f"{_describe(capture, n)}   {trace.volts.min():.3g} … "
                     f"{trace.volts.max():.3g} V")
        if not shared:
            zero = float(place(n, 0.0))
            if -_HALF <= zero <= _HALF:
                ax.plot([0], [zero], marker=">", markersize=15, color=color,
                        transform=ax.get_yaxis_transform(), clip_on=False)
                ax.text(0.003, zero, str(n), color=SCREEN, fontsize=8, fontweight="bold",
                        va="center", ha="center", transform=ax.get_yaxis_transform())
        if trigger.source == f"ANALOG{n}":
            ax.axhline(float(place(n, trigger.level_v)), color=MUTED, linewidth=0.8,
                       linestyle="--")

    legend = ax.legend(loc="upper right", fontsize=8, frameon=True, facecolor=SCREEN,
                       edgecolor=GRID, labelcolor=INK)
    legend.get_frame().set_alpha(0.85)


def _draw_digital(ax, capture: Capture, scale: float) -> None:
    channels = list(capture.digital)
    for row, channel in enumerate(reversed(channels)):
        trace = capture.digital[channel]
        times, levels = _transitions(trace.time_s, trace.levels)
        ax.step(times / scale, levels * 0.7 + row, where="post", color=LOGIC, linewidth=1.0)
    ax.set_ylim(-0.3, len(channels))
    ax.set_yticks([row + 0.35 for row in range(len(channels))])
    ax.set_yticklabels([f"D{n}" for n in reversed(channels)])
    ax.grid(axis="y", visible=False)
    ax.set_ylabel("logic", color=MUTED)


class _DivisionLocator(MaxNLocator):
    """Time ticks on the scope's divisions, counted from the trigger.

    Right for a record longer than the screen too. Zoomed in closer than a
    few divisions, it falls back to ordinary ticks, so a close-up is still
    labelled.
    """

    def __init__(self, per_div: float) -> None:
        """Take the division width, in the plot's time unit."""
        super().__init__(nbins=10, steps=[1, 2, 2.5, 5, 10])
        self._divisions = MultipleLocator(per_div)
        self._per_div = per_div

    def tick_values(self, vmin: float, vmax: float):
        """Divisions when at least four are in view, otherwise automatic ticks."""
        if 4 <= abs(vmax - vmin) / self._per_div <= 60:
            return self._divisions.tick_values(vmin, vmax)
        return super().tick_values(vmin, vmax)


def _trace(ax, times: np.ndarray, values: np.ndarray, *, color: str, label: str | None = None):
    """Draw an analog trace, thinned to an envelope when it is long.

    The thinning is redone for the visible range every time it changes, so
    zooming in on a million-point record shows its real samples.
    """
    (line,) = ax.plot([], [], color=color, linewidth=1.2, label=label)
    times, values = np.asarray(times), np.asarray(values)

    def refresh(axes) -> None:
        low, high = axes.get_xlim()
        line.set_data(*envelope(times, values, low, high))

    refresh(ax)
    if len(times) > 2 * ENVELOPE_BINS:
        ax.callbacks.connect("xlim_changed", refresh)
    return line


def envelope(times: np.ndarray, values: np.ndarray, low: float, high: float,
             bins: int = ENVELOPE_BINS) -> tuple[np.ndarray, np.ndarray]:
    """Return the samples between ``low`` and ``high``, thinned for drawing.

    Up to ``2 * bins`` samples come back untouched. Beyond that, each bin
    contributes its minimum and its maximum --- so a spike one sample wide
    still reaches its full height --- drawn at the bin's first and last time.
    """
    first = max(int(np.searchsorted(times, low)) - 1, 0)
    last = min(int(np.searchsorted(times, high)) + 1, len(times))
    times, values = times[first:last], values[first:last]
    if len(times) <= 2 * bins:
        return times, values
    per_bin = len(times) // bins
    usable = per_bin * bins
    t = times[:usable].reshape(bins, per_bin)
    v = values[:usable].reshape(bins, per_bin)
    thin_t = np.column_stack((t[:, 0], t[:, -1])).ravel()
    thin_v = np.column_stack((v.min(axis=1), v.max(axis=1))).ravel()
    return (np.concatenate((thin_t, times[usable:])),
            np.concatenate((thin_v, values[usable:])))


def _transitions(times: np.ndarray, levels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Keep only the samples where a logic level changes, plus both ends.

    Drawn as steps, this is exactly the original trace, at any length.
    """
    if len(levels) < 3:
        return times, levels
    changes = np.flatnonzero(np.diff(levels.astype(np.int8))) + 1
    keep = np.concatenate(([0], changes, [len(levels) - 1]))
    return times[keep], levels[keep]


def _describe(capture: Capture, channel: int) -> str:
    setup = capture.settings.analog[channel]
    return (f"ch{channel}  {_si(setup.volts_per_div, 'V')}/div  offset {setup.offset_v:g} V  "
            f"{setup.coupling}  probe {setup.probe}")


def _style(ax) -> None:
    ax.set_facecolor(SCREEN)
    ax.grid(color=GRID, linewidth=0.7)
    ax.tick_params(colors=MUTED, labelsize=8)
    for spine in ax.spines.values():
        spine.set_color(GRID)


def _time_span(capture: Capture) -> tuple[float, float]:
    """Start and end of the record, which is the screen for a NORMal readout."""
    traces = list(capture.analog.values()) + list(capture.digital.values())
    start = min(float(t.time_s[0]) for t in traces)
    stop = max(float(t.time_s[-1]) for t in traces)
    increment = min((float(t.time_s[1] - t.time_s[0]) for t in traces if len(t.time_s) > 1),
                    default=0.0)
    return start, stop + increment


def time_axis(capture: Capture) -> tuple[float, str]:
    """The time unit the plot uses for this capture: ``(seconds per unit, name)``.

    E.g. ``(1e-3, "ms")``. Multiply the plot's x values by the first element
    to get seconds --- what a caller needs to turn a zoomed view into a time
    window.
    """
    start, stop = _time_span(capture)
    span = stop - start
    for scale, unit in _UNITS:
        if span >= 2 * scale:
            return scale, unit
    return _UNITS[-1]


def _si(value: float, unit: str) -> str:
    """Format with an SI prefix: 0.0002 s -> '200 µs'."""
    for factor, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")):
        if abs(value) >= factor:
            return f"{value / factor:g} {prefix}{unit}"
    return f"{value / 1e-12:g} p{unit}"
