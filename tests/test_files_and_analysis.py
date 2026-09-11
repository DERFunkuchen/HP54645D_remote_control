# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Loading saved captures back, and measuring them."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from hp54645d import ScopeError, load_capture, measure
from hp54645d.analysis import Measurement, measure_analog, measure_digital
from hp54645d.gui import format_measurement
from hp54645d.settings import Settings
from hp54645d.waveform import AnalogTrace, Capture, DigitalTrace, Preamble


def square(points: int = 20_000, *, frequency: float = 1000.0, duty: float = 0.25,
           noise: float = 0.05) -> Capture:
    """0-5 V at ``frequency``, 1 us per sample from -10 ms, with a little noise."""
    rng = np.random.default_rng(1)
    pre = Preamble(0, 0, points, 1, 1e-6, -10e-3, 0, 0.0625, 2.5, 128)
    t = pre.times(points)
    phase = ((t - t[0]) * frequency) % 1.0
    volts = np.where(phase < duty, 5.0, 0.0) + rng.normal(0, noise, points)
    logic = (((t - t[0]) * 2 * frequency) % 1.0) < 0.5
    s = Settings()
    s.analog[1].displayed, s.analog[1].range_v, s.analog[1].offset_v = True, 16.0, 2.5
    s.timebase.range_s = 2e-3
    s.digital.displayed = (3,)
    return Capture({1: AnalogTrace(1, t, volts, pre)}, {3: DigitalTrace(3, t, logic)}, s,
                   "memory", datetime(2026, 9, 11, 14, 0, 0))


# -- loading ------------------------------------------------------------------------


def test_npz_comes_back_exactly(tmp_path) -> None:
    original = square()
    path = original.save(tmp_path / "run.npz")[0]
    loaded = load_capture(path)

    assert np.array_equal(loaded.analog[1].volts, original.analog[1].volts)
    assert np.array_equal(loaded.analog[1].time_s, original.analog[1].time_s)
    assert np.array_equal(loaded.digital[3].levels, original.digital[3].levels)
    assert loaded.settings == original.settings
    assert (loaded.origin, loaded.timestamp) == ("memory", original.timestamp)
    assert loaded.file == path


@pytest.mark.parametrize("open_which", ["run.csv", "run_digital.csv"])
def test_csv_comes_back_whichever_of_its_files_is_opened(tmp_path, open_which) -> None:
    original = square(2000)
    original.save(tmp_path / "run.csv")
    loaded = load_capture(tmp_path / open_which)

    assert loaded.analog[1].volts == pytest.approx(original.analog[1].volts, abs=1e-5)
    assert loaded.analog[1].time_s == pytest.approx(original.analog[1].time_s, abs=1e-12)
    assert np.array_equal(loaded.digital[3].levels, original.digital[3].levels)
    assert loaded.settings.analog[1].range_v == 16.0
    assert loaded.settings.timebase.range_s == 2e-3
    assert loaded.origin == "memory"


def test_a_csv_from_before_the_settings_line_still_loads(tmp_path) -> None:
    """Files saved earlier today carry only the per-channel scale in words."""
    old = tmp_path / "old.csv"
    old.write_text(
        "# HP 54645D screen, 2026-09-11T12:45:19\n"
        "# ch1: 16 V full screen, offset 2.5625 V\n"
        "time_s,ch1_v\n"
        "-0.001,0.0\n-0.0005,5.0625\n0.0,0.0\n0.0005,5.0625\n",
        encoding="utf-8",
    )
    loaded = load_capture(old)
    assert loaded.settings.analog[1].range_v == 16.0
    assert loaded.settings.analog[1].offset_v == 2.5625
    assert loaded.settings.analog[1].displayed
    assert loaded.settings.timebase.range_s == pytest.approx(1.5e-3)
    assert loaded.origin == "screen"


def test_something_else_is_refused(tmp_path) -> None:
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ScopeError, match="not an HP 54645D capture"):
        load_capture(other)
    with pytest.raises(ScopeError, match="no such file"):
        load_capture(tmp_path / "missing.npz")


# -- measuring ------------------------------------------------------------------------


def values(found: list[Measurement]) -> dict[str, float]:
    return {m.name: m.value for m in found}


def test_a_known_square_measures_as_built() -> None:
    """1 kHz, 25 % duty, 0-5 V: every answer is known in advance."""
    m = values(measure_analog(square().analog[1]))
    assert m["frequency"] == pytest.approx(1000, rel=1e-3)
    assert m["period"] == pytest.approx(1e-3, rel=1e-3)
    assert m["duty"] == pytest.approx(25, abs=0.5)
    assert m["mean"] == pytest.approx(1.25, abs=0.05)
    assert m["rms"] == pytest.approx(2.5, abs=0.05)      # sqrt(0.25 * 5**2)
    assert m["pk-pk"] == pytest.approx(5, abs=0.5)


def test_noise_on_the_edges_is_not_counted_as_edges() -> None:
    """Heavy noise, still 1 kHz: that is what the hysteresis is for."""
    m = values(measure_analog(square(noise=0.4).analog[1]))
    assert m["frequency"] == pytest.approx(1000, rel=1e-3)


def test_a_window_measures_only_what_is_inside_it() -> None:
    capture = square()
    whole = values(measure_analog(capture.analog[1]))
    high_part = values(measure_analog(capture.analog[1], start=-10e-3, stop=-9.8e-3))
    assert high_part["mean"] == pytest.approx(5, abs=0.1), "the first 200 us are all high"
    assert "frequency" not in high_part, "no edges in the window: no frequency, not a guess"
    assert whole["mean"] < high_part["mean"]


def test_logic_channels_are_measured_too() -> None:
    m = values(measure_digital(square().digital[3]))
    assert m["high"] == pytest.approx(50, abs=0.1)
    assert m["frequency"] == pytest.approx(2000, rel=1e-3)


def test_measure_covers_every_trace() -> None:
    assert list(measure(square())) == ["ch1", "D3"]


def test_a_million_points_measure_quickly() -> None:
    import time

    capture = square(1_000_000)
    started = time.perf_counter()
    measure(capture)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("measurement", "text"),
    [(Measurement("frequency", 1232.7, "Hz"), "1.2327 kHz"),
     (Measurement("period", 811.2e-6, "s"), "811.2 µs"),
     (Measurement("min", -0.0625, "V"), "-62.5 mV"),
     (Measurement("duty", 49.96, "%"), "50.0 %")],
)
def test_measurements_are_shown_with_si_prefixes(measurement, text) -> None:
    assert format_measurement(measurement) == text
