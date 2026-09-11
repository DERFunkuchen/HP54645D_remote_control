# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""The library against a simulated line. No hardware."""

from __future__ import annotations

import numpy as np
import pytest
from fakes import FakeScope

from hp54645d import HP54645D, CommandError, ScopeError, TriggerTimeout
from hp54645d.transport import Transport


def connect(fake: FakeScope) -> HP54645D:
    return HP54645D(transport=Transport(fake, baud_rate=19200))


# -- connecting ---------------------------------------------------------------


def test_connect_identifies_the_model_and_clears_old_errors() -> None:
    fake = FakeScope(errors=[(-113, "Undefined header")])
    scope = connect(fake)
    assert scope.model == "54645D"
    assert scope.has_digital
    assert fake.error_queue == [], "errors from before we connected must not be blamed on us"


def test_something_that_is_not_the_scope_is_refused() -> None:
    fake = FakeScope(answers={"*IDN?": "Agilent Technologies,34401A,0,1-1-1"})
    with pytest.raises(ScopeError, match="not an HP 54645"):
        connect(fake)
    assert fake.closed


# -- settings -----------------------------------------------------------------


def test_settings_are_parsed_from_the_real_answers() -> None:
    """Short-form answers (CENT, ANAL1, NORM,EDGE) become canonical names."""
    s = connect(FakeScope()).read_settings()

    assert s.analog[1].displayed and not s.analog[2].displayed
    assert s.analog[1].range_v == 16.0
    assert s.analog[1].volts_per_div == 2.0
    assert s.analog[1].probe == "X10"
    assert s.timebase.reference == "CENTER"
    assert s.timebase.seconds_per_div == pytest.approx(200e-6)
    assert (s.trigger.mode, s.trigger.type) == ("NORMAL", "EDGE")
    assert s.trigger.source == "ANALOG1"
    assert s.trigger.slope == "POSITIVE"
    assert s.digital.thresholds[1] == ("TTL", 1.4)
    assert s.acquire.type == "NORMAL"


def test_reading_settings_sends_only_queries() -> None:
    fake = FakeScope()
    connect(fake).read_settings()
    assert fake.commands == []


def test_configure_sends_probe_before_range_before_offset() -> None:
    """Probe rescales range and offset; the offset's limits depend on the range."""
    fake = FakeScope()
    connect(fake).configure_analog(2, offset_v=1.0, range_v=4.0, probe="x10", displayed=True)
    assert fake.commands == [
        ":ANALog2:PROBe X10",
        ":ANALog2:RANGe 4",
        ":ANALog2:OFFSet 1",
        ":VIEW ANALog2",
    ]


def test_configure_leaves_alone_what_it_is_not_given() -> None:
    fake = FakeScope()
    connect(fake).configure_timebase(range_s=2e-3)
    assert fake.commands == [":TIMebase:RANGe 0.002"]


def test_a_refused_command_raises_with_the_scope_s_message() -> None:
    fake = FakeScope()
    scope = connect(fake)
    fake.error_queue.append((-222, "Data out of range"))
    with pytest.raises(CommandError, match="-222 Data out of range") as info:
        scope.configure_analog(1, offset_v=1e9)
    assert info.value.errors == [(-222, "Data out of range")]


def test_trigger_mode_and_type_go_in_one_command() -> None:
    """Changing only the mode keeps the current type, read from the scope."""
    fake = FakeScope()
    connect(fake).configure_trigger(mode="auto", level_v=1.5)
    assert fake.commands == [":TRIGger:MODE AUTO,EDGE", ":TRIGger:EDGE:LEVel 1.5"]


def test_a_user_threshold_needs_its_voltage() -> None:
    scope = connect(FakeScope())
    with pytest.raises(ValueError, match="needs a voltage"):
        scope.configure_digital(thresholds={1: "USERDEF"})


def test_bad_choices_and_channels_are_caught_before_sending() -> None:
    fake = FakeScope()
    scope = connect(fake)
    with pytest.raises(ValueError, match="analog channel 3"):
        scope.configure_analog(3, range_v=1.0)
    with pytest.raises(ValueError, match="'AC/DC' is not one of"):
        scope.configure_analog(1, coupling="AC/DC")
    assert fake.commands == []


# -- the screenshot: read what is displayed, change nothing ------------------------


def test_read_screen_changes_nothing_on_the_scope() -> None:
    """The one promise of read_screen: no re-arm, no run/stop, no setting.

    Its only commands are the waveform transfer's own source, format and
    point-count selection, which are not visible on the instrument.
    """
    fake = FakeScope(displayed=("ANALog1", "DIGital0", "DIGital1"))
    capture = connect(fake).read_screen()

    assert capture.origin == "screen"
    assert all(c.startswith(":WAVeform:") for c in fake.commands), fake.commands
    assert list(capture.analog) == [1]
    assert list(capture.digital) == [0, 1]


def test_read_screen_converts_with_the_preamble() -> None:
    capture = connect(FakeScope()).read_screen()
    trace = capture.analog[1]
    # (87 - 128) * 0.0625 + 2.5625 = 0.0; (168 - 128) * 0.0625 + 2.5625 = 5.0625
    assert trace.volts == pytest.approx([0.0, 5.0625, 0.0, 5.0625])
    assert trace.time_s == pytest.approx([-1e-3, -0.5e-3, 0.0, 0.5e-3])


def test_digital_bits_are_split_per_channel() -> None:
    capture = connect(FakeScope(displayed=("DIGital0", "DIGital1"))).read_screen()
    assert capture.digital[0].levels.tolist() == [True, True, False, False]
    assert capture.digital[1].levels.tolist() == [False, True, True, False]


def test_read_screen_refuses_a_hidden_channel_without_asking_the_scope() -> None:
    """The scope never answers for a hidden source; asking would cost a timeout."""
    fake = FakeScope(displayed=("ANALog1",))
    with pytest.raises(ScopeError, match="ch2 not displayed"):
        connect(fake).read_screen(analog=[2])
    assert ":WAVeform:PREamble?" not in fake.sent


def test_fewer_points_are_asked_for_analog_sources_only() -> None:
    """Pods are read at their own length, the only one checked on the bench."""
    fake = FakeScope(displayed=("ANALog1", "DIGital0"))
    connect(fake).read_screen(points=500)
    assert ":WAVeform:POINts NORMal,500" in fake.commands
    source_then_points = list(zip(fake.commands, fake.commands[2:], strict=False))
    assert (":WAVeform:SOURce POD1", ":WAVeform:POINts NORMal") in source_then_points


def test_a_record_length_the_scope_does_not_offer_is_refused() -> None:
    with pytest.raises(ValueError, match="record length 300"):
        connect(FakeScope()).read_screen(points=300)


def test_reusing_settings_skips_reading_them() -> None:
    """What makes a live view affordable: a frame is transfers, nothing else."""
    fake = FakeScope()
    scope = connect(fake)
    cached = scope.read_settings()
    fake.sent.clear()

    scope.read_screen(settings=cached)

    queries = [c for c in fake.sent if c.endswith("?") or "? " in c]
    assert queries == [":WAVeform:PREamble?", ":WAVeform:DATA?", ":SYSTem:ERRor?"]


def test_read_screen_with_nothing_displayed() -> None:
    with pytest.raises(ScopeError, match="nothing is displayed"):
        connect(FakeScope(displayed=())).read_screen()


def test_read_screen_needs_the_main_timebase() -> None:
    fake = FakeScope(answers={":TIMebase:MODE?": "ROLL"})
    with pytest.raises(ScopeError, match="ROLL mode"):
        connect(fake).read_screen()


# -- acquiring ----------------------------------------------------------------


def test_acquire_arms_a_single_and_reads_every_channel_from_it() -> None:
    fake = FakeScope(displayed=("ANALog1",), trigger_events=["+0", "+0", "+1"])
    capture = connect(fake).acquire(analog=[1, 2], digital=[0], timeout_s=5)

    assert capture.origin == "acquisition"
    assert list(capture.analog) == [1, 2] and list(capture.digital) == [0]
    commands = fake.commands
    # Hidden channels shown first, then stop / single -- and never :DIGitize,
    # which blocks the parser until a trigger arrives.
    assert commands[:2] == [":VIEW ANALog2", ":VIEW DIGital0"]
    assert commands[2:4] == [":STOP", ":SINGle"]
    assert not any(c.startswith(":DIGitize") for c in commands)


def test_acquire_gives_up_and_stops_when_nothing_triggers() -> None:
    fake = FakeScope(trigger_events=["+0"])
    with pytest.raises(TriggerTimeout, match="no trigger within 0.2 s"):
        connect(fake).acquire(timeout_s=0.2)
    assert fake.commands[-1] == ":STOP", "the pending single must be stopped"


def test_acquire_needs_something_to_acquire() -> None:
    with pytest.raises(ValueError, match="nothing to acquire"):
        connect(FakeScope()).acquire(analog=[], digital=[])


# -- the line -----------------------------------------------------------------


def test_a_data_byte_equal_to_newline_does_not_end_the_block() -> None:
    fake = FakeScope()
    fake.source = "ANALog1"
    transport = Transport(fake)
    fake._pending += b"#800000004" + bytes([128, 10, 10, 128]) + b"\n"  # noqa: SLF001
    assert transport.query_block(":WAVeform:DATA?") == bytes([128, 10, 10, 128])


def test_a_short_block_is_a_timeout_not_a_short_record() -> None:
    fake = FakeScope()
    transport = Transport(fake)
    fake._pending += b"#800000004" + bytes([128, 128, 128])  # noqa: SLF001 - one byte lost
    fake.source = "POD2"  # so the fake's DATA? adds nothing more
    fake.write = lambda command: None
    from hp54645d import ScopeTimeout

    with pytest.raises(ScopeTimeout):
        transport.query_block(":WAVeform:DATA?")


def test_the_error_queue_is_read_until_empty() -> None:
    queued = [(-113, "Undefined header"), (-221, "Settings conflict")]
    transport = Transport(FakeScope(errors=list(queued)))
    assert transport.errors() == queued
    assert transport.errors() == []


# -- results --------------------------------------------------------------------


def test_save_csv_writes_analog_and_digital_side_by_side(tmp_path) -> None:
    capture = connect(FakeScope(displayed=("ANALog1", "DIGital0"))).read_screen()
    written = capture.save_csv(tmp_path / "cap.csv")

    assert [p.name for p in written] == ["cap.csv", "cap_digital.csv"]
    analog = (tmp_path / "cap.csv").read_text(encoding="utf-8").splitlines()
    assert analog[0].startswith("# HP 54645D screen")
    assert "time_s,ch1_v" in analog
    digital = tmp_path / "cap_digital.csv"
    header = sum(line.startswith("#") for line in digital.read_text("utf-8").splitlines()) + 1
    data = np.loadtxt(digital, delimiter=",", skiprows=header)
    assert data[:, 1].tolist() == [1, 1, 0, 0]


def test_plot_draws_one_panel_per_analog_channel_plus_logic() -> None:
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    from hp54645d import plot_capture

    capture = connect(FakeScope(displayed=("ANALog1", "ANALog2", "DIGital0"))).read_screen()
    figure = plot_capture(capture, Figure())
    assert len(figure.axes) == 3
    assert figure.axes[0].get_ylim() == pytest.approx((2.5625 - 8, 2.5625 + 8))


def _overlay(answers: dict[str, str] | None = None):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib.figure import Figure

    from hp54645d import plot_capture

    fake = FakeScope(displayed=("ANALog1", "ANALog2", "DIGital0"), answers=answers)
    capture = connect(fake).read_screen()
    return plot_capture(capture, Figure(), layout="overlay")


def test_overlay_puts_both_channels_on_one_screen_in_divisions() -> None:
    """Different scales (16 V vs 0.8 V): one graticule, each channel by its own V/div."""
    figure = _overlay()
    analog, logic = figure.axes
    assert analog.get_ylabel() == "divisions"
    assert analog.get_ylim() == (-4.0, 4.0)
    assert len(analog.get_legend().get_texts()) == 2, "identity must not rest on colour alone"
    # ch1's 5.0625 V at 2 V/div around 2.5625 V sits 1.25 divisions up.
    ch1 = next(line for line in analog.lines if line.get_label().startswith("ch1"))
    assert max(ch1.get_ydata()) == pytest.approx(1.25)


def test_overlay_stays_in_volts_when_the_scales_match() -> None:
    same = {":ANALog2:RANGe?": "+1.60000E+01", ":ANALog2:OFFSet?": "+2.56250E+00"}
    analog = _overlay(same).axes[0]
    assert analog.get_ylabel() == "volts"
    assert analog.get_ylim() == pytest.approx((2.5625 - 8, 2.5625 + 8))


def test_an_unknown_layout_is_refused() -> None:
    from hp54645d import plot_capture

    capture = connect(FakeScope()).read_screen()
    with pytest.raises(ValueError, match="unknown layout"):
        plot_capture(capture, layout="stacked")
