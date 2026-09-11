# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""apply(), the live reader, the full-memory readout, saving, and plot thinning."""

from __future__ import annotations

import copy
import json

import matplotlib
import numpy as np
import pytest
from fakes import FakeScope

matplotlib.use("Agg")

from matplotlib.figure import Figure  # noqa: E402

from hp54645d import HP54645D, ApplyError, plot_capture  # noqa: E402
from hp54645d.plot import envelope  # noqa: E402
from hp54645d.settings import Settings  # noqa: E402
from hp54645d.transport import Transport  # noqa: E402
from hp54645d.waveform import AnalogTrace, Capture, DigitalTrace, Preamble  # noqa: E402


def connect(fake: FakeScope) -> HP54645D:
    return HP54645D(transport=Transport(fake, baud_rate=19200))


# -- apply --------------------------------------------------------------------------


def test_apply_sends_only_what_differs_and_reads_back() -> None:
    fake = FakeScope()
    scope = connect(fake)
    current = scope.read_settings()
    wanted = copy.deepcopy(current)
    wanted.analog[1].range_v = 8.0
    wanted.trigger.level_v = 1.0
    fake.sent.clear()

    after = scope.apply(wanted, current=current)

    assert fake.commands == [":ANALog1:RANGe 8", ":TRIGger:EDGE:LEVel 1"], \
        "channels before the trigger: the level's limits depend on the range"
    assert isinstance(after, Settings)
    assert ":ANALog1:RANGe?" in fake.sent, "settings are read back afterwards"


def test_apply_with_nothing_to_change_touches_nothing() -> None:
    fake = FakeScope()
    scope = connect(fake)
    current = scope.read_settings()
    fake.sent.clear()
    assert scope.apply(copy.deepcopy(current), current=current) is current
    assert fake.sent == []


def test_a_refusal_still_sends_the_rest_and_reports_what_the_scope_has() -> None:
    fake = FakeScope()
    scope = connect(fake)
    current = scope.read_settings()
    wanted = copy.deepcopy(current)
    wanted.analog[1].offset_v = 1e9
    wanted.timebase.range_s = 5e-3

    real_write = fake.write

    def refuse_offset(command: str) -> None:
        real_write(command)
        if command.startswith(":ANALog1:OFFSet"):
            fake.error_queue.append((-222, "Data out of range"))

    fake.write = refuse_offset
    with pytest.raises(ApplyError, match="channel 1") as info:
        scope.apply(wanted, current=current)
    assert ":TIMebase:RANGe 0.005" in fake.commands, "the timebase was still sent"
    assert isinstance(info.value.settings, Settings)
    assert "-222 Data out of range" in info.value.failures[0]


# -- live -----------------------------------------------------------------------------


def settings_reads(fake: FakeScope) -> int:
    return fake.sent.count(":ANALog1:RANGe?")


def test_live_reads_settings_once_then_only_transfers() -> None:
    fake = FakeScope()
    live = connect(fake).live(points=500)
    fake.sent.clear()
    for _ in range(3):
        live.read()
    assert settings_reads(fake) == 1
    assert live.frames == 3


def test_live_rereads_settings_when_they_are_old() -> None:
    fake = FakeScope()
    live = connect(fake).live(refresh_s=0.0)
    fake.sent.clear()
    live.read()
    live.read()
    assert settings_reads(fake) == 2


def test_live_recovers_when_the_display_changes_underneath_it() -> None:
    """ch1 hidden on the front panel, ch2 shown: the next frame follows."""
    fake = FakeScope(displayed=("ANALog1",))
    live = connect(fake).live()
    assert list(live.read().analog) == [1]

    fake.displayed = {"ANALog2"}
    frame = live.read()
    assert list(frame.analog) == [2]


def test_live_adopts_settings_it_is_handed() -> None:
    fake = FakeScope()
    scope = connect(fake)
    live = scope.live()
    live.use(scope.read_settings())
    fake.sent.clear()
    live.read()
    assert settings_reads(fake) == 0


def test_live_iterates() -> None:
    frames = []
    for capture in connect(FakeScope()).live():
        frames.append(capture)
        if len(frames) == 2:
            break
    assert len(frames) == 2


# -- full memory ------------------------------------------------------------------


def test_read_memory_stops_first_then_reads_every_point() -> None:
    fake = FakeScope(displayed=("ANALog1", "DIGital0"))
    seen = []
    capture = connect(fake).read_memory(progress=lambda *args: seen.append(args))

    assert fake.commands[0] == ":STOP", "the memory must not change during the transfer"
    assert ":WAVeform:POINts ALL" in fake.commands
    assert capture.origin == "memory"
    assert list(capture.analog) == [1] and list(capture.digital) == [0]
    assert {source for source, _, _ in seen} == {"ANALOG1", "POD1"}
    assert seen[-1][1] == seen[-1][2], "progress ends at the total"


def test_memory_sizes_come_from_the_preambles() -> None:
    fake = FakeScope(displayed=("ANALog1", "DIGital3"))
    assert connect(fake).memory_sizes() == {"ANALOG1": 4, "POD1": 4}
    assert all(c.startswith(":WAVeform:") for c in fake.commands), "sizing changes nothing"


def test_a_long_block_is_read_in_chunks_with_progress() -> None:
    fake = FakeScope()
    fake.timeout = 50  # ms: at 19200 baud, chunks of the 64-byte minimum
    transport = Transport(fake, baud_rate=19200)
    data = bytes(range(200))
    fake._pending += b"#8" + b"00000200" + data + b"\n"  # noqa: SLF001
    fake.write = lambda command: None
    seen = []
    assert transport.query_block(":WAVeform:DATA?", lambda d, t: seen.append(d)) == data
    assert seen == [64, 128, 192, 200]


class Streaming:
    """A line still delivering the rest of an abandoned block."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Queue up what is still to arrive."""
        self.chunks = chunks
        self.timeout = 1000

    @property
    def bytes_in_buffer(self) -> int:
        """What has arrived and not been read."""
        return len(self.chunks[0]) if self.chunks else 0

    def read_bytes(self, count: int) -> bytes:
        """Hand over the next chunk."""
        return self.chunks.pop(0)


def test_drain_waits_for_silence_not_for_a_newline() -> None:
    """Binary data may never contain a newline; the line going quiet is the end."""
    line = Streaming([b"\x00" * 4096, b"\x13" * 4096, b"\x80" * 100])
    assert Transport(line).drain(quiet_s=0.1) == 8292
    assert line.chunks == []


# -- saving ---------------------------------------------------------------------------


def long_capture(points: int = 1_000_000, *, digital: bool = False) -> Capture:
    """A synthetic full-memory record: 5 ns per point, a spike at one sample."""
    pre = Preamble(0, 0, points, 1, 5e-9, -1e-3, 0, 0.0625, 2.5, 128)
    t = pre.times(points)
    v = np.where((np.arange(points) // 100_000) % 2 == 0, 0.0, 5.0)
    v[123_457 if points > 123_457 else points // 3] = 12.0
    traces = {1: AnalogTrace(1, t, v, pre)}
    logic = {}
    if digital:
        levels = (np.arange(points) // 1000) % 2 == 1
        logic = {0: DigitalTrace(0, t, levels)}
    s = Settings()
    s.analog[1].displayed = True
    s.analog[1].range_v, s.analog[1].offset_v = 16.0, 2.5
    s.timebase.range_s = 2e-3
    return Capture(traces, logic, s, "memory")


def test_npz_holds_everything_and_the_settings(tmp_path) -> None:
    capture = long_capture(10_000, digital=True)
    path = capture.save(tmp_path / "mem.npz")[0]
    with np.load(path) as saved:
        assert np.array_equal(saved["ch1_v"], capture.analog[1].volts)
        assert np.array_equal(saved["d0"], capture.digital[0].levels.astype(np.uint8))
        meta = json.loads(str(saved["meta"]))
    assert meta["origin"] == "memory"
    assert meta["settings"]["analog"]["1"]["range_v"] == 16.0


def test_csv_of_a_long_record(tmp_path) -> None:
    capture = long_capture(50_000)
    path = capture.save(tmp_path / "mem.csv")[0]
    header = sum(line.startswith("#") for line in path.read_text("utf-8").splitlines()) + 1
    data = np.loadtxt(path, delimiter=",", skiprows=header)
    assert data.shape == (50_000, 2)
    assert data[:, 0] == pytest.approx(capture.analog[1].time_s, abs=1e-12)


def test_analog_channels_of_different_lengths_get_their_own_files(tmp_path) -> None:
    capture = long_capture(1000)
    pre = Preamble(0, 0, 500, 1, 1e-8, -1e-3, 0, 1, 0, 128)
    capture.analog[2] = AnalogTrace(2, pre.times(500), np.zeros(500), pre)
    names = [p.name for p in capture.save_csv(tmp_path / "cap.csv")]
    assert names == ["cap_ch1.csv", "cap_ch2.csv"]


# -- plotting a million points -----------------------------------------------------------


def test_the_envelope_keeps_a_one_sample_spike() -> None:
    capture = long_capture()
    trace = capture.analog[1]
    t, v = envelope(trace.time_s, trace.volts, trace.time_s[0], trace.time_s[-1])
    assert len(t) <= 2 * 3000 + 1000
    assert v.max() == 12.0, "a spike one sample wide must still reach its height"


def test_zooming_in_shows_the_real_samples() -> None:
    capture = long_capture()
    figure = plot_capture(capture, Figure())
    ax = figure.axes[0]
    line = next(line for line in ax.lines if len(line.get_xdata()) > 2)
    assert len(line.get_xdata()) <= 7000, "thinned at full view"

    trace = capture.analog[1]
    window = trace.time_s[123_400:123_500] * 1e3  # the plot is in ms here
    ax.set_xlim(window[0], window[-1])
    assert len(line.get_xdata()) >= 100, "the real samples at close range"
    assert max(line.get_ydata()) == 12.0


def test_logic_is_drawn_from_its_transitions_exactly() -> None:
    figure = plot_capture(long_capture(100_000, digital=True), Figure())
    logic = figure.axes[-1]
    steps = max((line for line in logic.lines), key=lambda line: len(line.get_xdata()))
    assert len(steps.get_xdata()) == 101, "100 transitions plus both ends"
