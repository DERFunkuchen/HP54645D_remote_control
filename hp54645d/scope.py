# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""The HP 54645D: settings, run control, and reading waveforms.

Three ways to get data, and the difference matters:

:meth:`HP54645D.read_screen`
    Reads what the scope is showing **now**. Sends no command that changes
    anything --- no re-arm, no run/stop, no settings --- only queries and the
    waveform transfer's own source/format selection. The digital equivalent of
    photographing the screen. :meth:`HP54645D.live` repeats it.

:meth:`HP54645D.acquire`
    Takes a **new** triggered capture: switches on any channel asked for,
    arms a single acquisition, waits for the trigger and reads the result.
    Leaves the scope stopped, showing that capture.

:meth:`HP54645D.read_memory`
    Reads **every stored point** of a stopped scope --- up to a million per
    analog channel, several screens' worth at the full sample rate. Minutes
    per channel over RS-232.

Command references are to the programmer's guide in ``docs/manuals/``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Mapping

from hp54645d.errors import ApplyError, ScopeError, TriggerTimeout
from hp54645d.live import LiveReader
from hp54645d.settings import (
    ACQUIRE_TYPE,
    ANALOG_CHANNELS,
    AVERAGE_COUNTS,
    COUPLING,
    DIGITAL_CHANNELS,
    NORMAL_POINTS,
    PODS,
    PROBE,
    REFERENCE,
    REJECT,
    SLOPE,
    THRESHOLD,
    TIMEBASE_MODE,
    TRIGGER_COUPLING,
    TRIGGER_MODE,
    TRIGGER_SOURCE,
    TRIGGER_TYPE,
    AcquireSettings,
    AnalogSettings,
    DigitalSettings,
    Settings,
    TimebaseSettings,
    TriggerSettings,
    changes,
    parse_bool,
    pod_of,
)
from hp54645d.transport import Transport
from hp54645d.waveform import (
    AnalogTrace,
    Capture,
    DigitalTrace,
    Preamble,
    decode_analog,
    decode_pod,
)

_POLL_S = 0.05


class HP54645D:
    """An HP 54645A/D oscilloscope over RS-232 (or HP-IB).

    Example:
        ```python
        from hp54645d import HP54645D

        with HP54645D("ASRL5::INSTR", baud_rate=19200) as scope:
            capture = scope.read_screen()
        ```
    """

    def __init__(
        self,
        address: str = "ASRL5::INSTR",
        *,
        baud_rate: int = 19200,
        flow_control: str = "xon_xoff",
        timeout_s: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        """Connect and confirm it is an HP 54645.

        The line settings must match the scope's I/O menu; it cannot report
        them, and a mismatch gives silence rather than an error.

        Args:
            address: VISA resource: ``"ASRL5::INSTR"`` is COM5.
            baud_rate: 1200, 2400, 9600 or 19200.
            flow_control: ``"xon_xoff"`` (the scope's *XON* setting; the only
                one that works on a three-wire cable) or ``"dtr_dsr"``.
            timeout_s: Default I/O timeout.
            transport: An already-open :class:`~hp54645d.transport.Transport`;
                for tests. The other arguments are then ignored.

        Raises:
            ScopeTimeout: If nothing answers --- see the message.
            ScopeError: If something answers that is not an HP 54645.
        """
        self._io = transport or Transport.open(
            address, baud_rate=baud_rate, flow_control=flow_control, timeout_s=timeout_s
        )
        self.address = address
        try:
            # Anything still arriving is left over from an earlier session ---
            # a transfer abandoned mid-way keeps coming until the scope has
            # sent all of it. Wait for silence before the first question.
            self._io.drain()
            self.identity = self._io.query("*IDN?")
        except ScopeError as exc:
            self._io.close()
            raise type(exc)(
                f"{exc}. Check the address and baud rate, and that the scope's "
                f"interface is set to 'Connect to Computer', not a printer."
            ) from exc
        if "54645" not in self.identity:
            self._io.close()
            raise ScopeError(f"{address} is not an HP 54645: {self.identity!r}")
        self.model = self.identity.split(",")[1].strip()
        self._io.errors()  # start clean, so later checks are about our commands

    def __repr__(self) -> str:
        """Return the model and address."""
        return f"<HP{self.model} at {self.address}>"

    def __enter__(self) -> HP54645D:
        """Use as a context manager; already connected."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Disconnect."""
        self.close()

    def close(self) -> None:
        """Disconnect. Leaves the scope as it is."""
        self._io.close()

    @property
    def has_digital(self) -> bool:
        """Whether this is the mixed-signal model, with 16 logic inputs."""
        return self.model.upper().endswith("D")

    # -- raw access ---------------------------------------------------------

    def write(self, command: str) -> None:
        """Send any command, and raise if the scope reports an error."""
        self._io.write(command)
        self._io.check(command)

    def query(self, command: str) -> str:
        """Send any query and return the answer."""
        return self._io.query(command)

    def errors(self) -> list[tuple[int, str]]:
        """Read and empty the scope's error queue."""
        return self._io.errors()

    # -- reading settings ---------------------------------------------------

    def read_settings(self) -> Settings:
        """Read every setting this package models. Queries only; about a second."""
        q = self._io.query
        analog = {}
        for n in ANALOG_CHANNELS:
            analog[n] = AnalogSettings(
                channel=n,
                displayed=parse_bool(q(f":STATus? ANALog{n}")),
                range_v=float(q(f":ANALog{n}:RANGe?")),
                offset_v=float(q(f":ANALog{n}:OFFSet?")),
                coupling=COUPLING.canonical(q(f":ANALog{n}:COUPling?")),
                probe=PROBE.canonical(q(f":ANALog{n}:PROBe?")),
                bandwidth_limit=parse_bool(q(f":ANALog{n}:BWLimit?")),
                invert=parse_bool(q(f":ANALog{n}:INVert?")),
            )

        timebase = TimebaseSettings(
            mode=TIMEBASE_MODE.canonical(q(":TIMebase:MODE?")),
            range_s=float(q(":TIMebase:RANGe?")),
            delay_s=float(q(":TIMebase:DELay?")),
            reference=REFERENCE.canonical(q(":TIMebase:REFerence?")),
        )

        mode, _, kind = q(":TRIGger:MODE?").partition(",")
        trigger = TriggerSettings(
            mode=TRIGGER_MODE.canonical(mode),
            type=TRIGGER_TYPE.canonical(kind or "EDGE"),
            source=TRIGGER_SOURCE.canonical(q(":TRIGger:EDGE:SOURce?")),
            level_v=float(q(":TRIGger:EDGE:LEVel?")),
            slope=SLOPE.canonical(q(":TRIGger:EDGE:SLOPe?")),
            coupling=TRIGGER_COUPLING.canonical(q(":TRIGger:COUPling?")),
            reject=REJECT.canonical(q(":TRIGger:REJect?")),
            holdoff_s=float(q(":TRIGger:HOLDoff?")),
            noise_reject=parse_bool(q(":TRIGger:NREJect?")),
        )

        digital = DigitalSettings(displayed=(), thresholds={})
        if self.has_digital:
            digital.displayed = tuple(
                n for n in DIGITAL_CHANNELS if parse_bool(q(f":STATus? DIGital{n}"))
            )
            for pod in PODS:
                kind, _, volts = q(f":CHANnel:THReshold? POD{pod}").partition(",")
                digital.thresholds[pod] = (THRESHOLD.canonical(kind), float(volts or "nan"))

        acquire = AcquireSettings(
            type=ACQUIRE_TYPE.canonical(q(":ACQuire:TYPE?")),
            count=int(float(q(":ACQuire:COUNt?"))),
        )
        return Settings(analog, timebase, trigger, digital, acquire)

    # -- changing settings --------------------------------------------------
    #
    # Each configure_* sends only what it is given --- None leaves a setting
    # alone --- and then reads the error queue, so a value the scope refused
    # raises CommandError with the scope's own message instead of passing
    # silently.

    def configure_analog(
        self,
        channel: int,
        *,
        displayed: bool | None = None,
        range_v: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        probe: str | None = None,
        bandwidth_limit: bool | None = None,
        invert: bool | None = None,
    ) -> None:
        """Change an analog channel (§8-4).

        Args:
            channel: 1 or 2.
            displayed: Show or hide the trace.
            range_v: Full-screen span, in volts (8 divisions).
            offset_v: Voltage at centre screen.
            coupling: ``"AC"``, ``"DC"`` or ``"GND"``.
            probe: ``"X1"``, ``"X10"``, ``"X20"`` or ``"X100"``. Scales every
                reading; set it to match the probe fitted.
            bandwidth_limit: Low-pass filter in or out.
            invert: Invert the trace.

        Raises:
            ValueError: For a channel or choice that does not exist.
            CommandError: If the scope refused a value.
        """
        _check_member(channel, ANALOG_CHANNELS, "analog channel")
        prefix = f":ANALog{channel}"
        commands = []
        # Probe first: it rescales range and offset. Range before offset: the
        # offset's limits depend on the range.
        if probe is not None:
            commands.append(f"{prefix}:PROBe {PROBE.command(probe)}")
        if coupling is not None:
            commands.append(f"{prefix}:COUPling {COUPLING.command(coupling)}")
        if range_v is not None:
            commands.append(f"{prefix}:RANGe {_number(range_v)}")
        if offset_v is not None:
            commands.append(f"{prefix}:OFFSet {_number(offset_v)}")
        if bandwidth_limit is not None:
            commands.append(f"{prefix}:BWLimit {_on_off(bandwidth_limit)}")
        if invert is not None:
            commands.append(f"{prefix}:INVert {_on_off(invert)}")
        if displayed is not None:
            commands.append(f"{':VIEW' if displayed else ':BLANk'} ANALog{channel}")
        self._send(commands, f"channel {channel}")

    def configure_timebase(
        self,
        *,
        mode: str | None = None,
        range_s: float | None = None,
        delay_s: float | None = None,
        reference: str | None = None,
    ) -> None:
        """Change the horizontal axis (§8-12).

        Args:
            mode: ``"MAIN"``, ``"DELAYED"``, ``"XY"`` or ``"ROLL"``.
            range_s: Full-screen span, in seconds (10 divisions); 50 ns to
                500 s on the 54645D.
            delay_s: Time from the trigger to the display reference.
            reference: ``"LEFT"``, ``"CENTER"`` or ``"RIGHT"``.
        """
        commands = []
        if mode is not None:
            commands.append(f":TIMebase:MODE {TIMEBASE_MODE.command(mode)}")
        if range_s is not None:
            commands.append(f":TIMebase:RANGe {_number(range_s)}")
        if reference is not None:
            commands.append(f":TIMebase:REFerence {REFERENCE.command(reference)}")
        if delay_s is not None:
            commands.append(f":TIMebase:DELay {_number(delay_s)}")
        self._send(commands, "timebase")

    def configure_trigger(
        self,
        *,
        mode: str | None = None,
        type: str | None = None,  # noqa: A002 - the instrument's own word
        source: str | None = None,
        level_v: float | None = None,
        slope: str | None = None,
        coupling: str | None = None,
        reject: str | None = None,
        holdoff_s: float | None = None,
        noise_reject: bool | None = None,
    ) -> None:
        """Change the trigger (§8-13, 8-14). Edge parameters only.

        Args:
            mode: ``"AUTLEVEL"``, ``"AUTO"`` or ``"NORMAL"``.
            type: ``"EDGE"``, ``"TV"``, ``"GLITCH"``, ``"ADVANCED"`` or
                ``"PATTERN"``.
            source: ``"ANALOG1"``, ``"ANALOG2"``, ``"DIGITAL0"`` …
                ``"DIGITAL15"`` or ``"LINE"``.
            level_v: Edge level: within ±0.75 × full screen of centre screen
                for an analog source, ±6 V for a digital one.
            slope: ``"POSITIVE"`` or ``"NEGATIVE"``.
            coupling: ``"AC"`` or ``"DC"``.
            reject: ``"OFF"``, ``"LF"`` or ``"HF"``.
            holdoff_s: 200 ns to 20 s.
            noise_reject: Noise reject on or off.
        """
        commands = []
        if mode is not None or type is not None:
            # One command sets both; fill in whichever was not given.
            current_mode, _, current_type = self._io.query(":TRIGger:MODE?").partition(",")
            new_mode = TRIGGER_MODE.command(mode if mode is not None else current_mode)
            new_type = TRIGGER_TYPE.command(type if type is not None else current_type or "EDGE")
            commands.append(f":TRIGger:MODE {new_mode},{new_type}")
        if source is not None:
            commands.append(f":TRIGger:EDGE:SOURce {TRIGGER_SOURCE.command(source)}")
        if slope is not None:
            commands.append(f":TRIGger:EDGE:SLOPe {SLOPE.command(slope)}")
        if level_v is not None:
            commands.append(f":TRIGger:EDGE:LEVel {_number(level_v)}")
        if coupling is not None:
            commands.append(f":TRIGger:COUPling {TRIGGER_COUPLING.command(coupling)}")
        if reject is not None:
            commands.append(f":TRIGger:REJect {REJECT.command(reject)}")
        if holdoff_s is not None:
            commands.append(f":TRIGger:HOLDoff {_number(holdoff_s)}")
        if noise_reject is not None:
            commands.append(f":TRIGger:NREJect {_on_off(noise_reject)}")
        self._send(commands, "trigger")

    def configure_digital(
        self,
        *,
        displayed: Mapping[int, bool] | None = None,
        thresholds: Mapping[int, str | tuple[str, float]] | None = None,
    ) -> None:
        """Change the logic inputs (54645D only).

        Args:
            displayed: Channel (0–15) to shown/hidden, e.g. ``{0: True, 1: True}``.
            thresholds: Pod (1 = D0–D7, 2 = D8–D15) to a logic family ---
                ``"TTL"``, ``"CMOS"``, ``"ECL"`` --- or ``("USERDEF", volts)``.
        """
        self._require_digital()
        commands = []
        for pod, spec in (thresholds or {}).items():
            _check_member(pod, PODS, "pod")
            kind, volts = (spec, None) if isinstance(spec, str) else spec
            command = f":CHANnel:THReshold POD{pod},{THRESHOLD.command(kind)}"
            if THRESHOLD.canonical(kind) == "USERDEF":
                if volts is None:
                    raise ValueError("a USERDEF threshold needs a voltage: ('USERDEF', 2.0)")
                command += f",{_number(volts)}"
            commands.append(command)
        for channel, show in (displayed or {}).items():
            _check_member(channel, DIGITAL_CHANNELS, "digital channel")
            commands.append(f"{':VIEW' if show else ':BLANk'} DIGital{channel}")
        self._send(commands, "digital channels")

    def configure_acquire(self, *, type: str | None = None, count: int | None = None) -> None:  # noqa: A002
        """Change the acquisition type, and the average count (§8-4).

        Args:
            type: ``"NORMAL"``, ``"AVERAGE"``, ``"PEAK"`` or ``"REALTIME"``.
            count: Averages: 4, 8, 16, 32, 64, 128 or 256.
        """
        commands = []
        if type is not None:
            commands.append(f":ACQuire:TYPE {ACQUIRE_TYPE.command(type)}")
        if count is not None:
            _check_member(count, AVERAGE_COUNTS, "average count")
            commands.append(f":ACQuire:COUNt {count}")
        self._send(commands, "acquisition")

    def apply(self, wanted: Settings, *, current: Settings | None = None) -> Settings:
        """Make the scope's settings match ``wanted``, sending only what differs.

        The difference is worked out by :func:`hp54645d.settings.changes` and
        sent section by section --- channels first, since the trigger level's
        limits depend on their ranges --- then everything is read back.

        A typical use: read, edit, apply::

            s = scope.read_settings()
            s.analog[1].range_v = 8.0
            s.trigger.level_v = 1.0
            s = scope.apply(s)

        Args:
            wanted: The settings to end up with. Usually a
                :meth:`read_settings` result, edited.
            current: The settings the scope has now, if just read; saves
                reading them again.

        Returns:
            The settings read back afterwards --- what the scope actually has,
            which can differ from ``wanted`` where it rounded or clamped a
            value. If nothing differed, ``current`` without any I/O.

        Raises:
            ApplyError: If some part was refused. The rest was still sent; the
                error carries the failures and the settings read back.
            ValueError: For a choice that does not exist; nothing is sent.
        """
        current = current or self.read_settings()
        diff = changes(current, wanted)
        if not diff:
            return current

        steps = [(f"channel {n}", lambda n=n, kw=kw: self.configure_analog(n, **kw))
                 for n, kw in diff.analog.items()]
        if diff.timebase:
            steps.append(("timebase", lambda: self.configure_timebase(**diff.timebase)))
        if diff.trigger:
            steps.append(("trigger", lambda: self.configure_trigger(**diff.trigger)))
        if diff.digital_displayed or diff.thresholds:
            steps.append(("digital", lambda: self.configure_digital(
                displayed=diff.digital_displayed, thresholds=diff.thresholds)))
        if diff.acquire:
            steps.append(("acquisition", lambda: self.configure_acquire(**diff.acquire)))

        failures = []
        for what, step in steps:
            try:
                step()
            except (ScopeError, ValueError) as exc:
                failures.append(f"{what}: {exc}")
        after = self.read_settings()
        if failures:
            raise ApplyError(failures, after)
        return after

    def live(
        self,
        analog: Iterable[int] | None = None,
        digital: Iterable[int] | None = None,
        *,
        points: int | None = 500,
        refresh_s: float = 10.0,
    ) -> LiveReader:
        """Repeated screen readouts, as fast as the line allows.

        ```python
        for capture in scope.live(points=500):
            ...   # about 0.55 s per channel at 19200 baud
        ```

        See :class:`~hp54645d.live.LiveReader` for how it keeps up.
        """
        return LiveReader(self, analog, digital, points=points, refresh_s=refresh_s)

    # -- run control --------------------------------------------------------

    def run(self) -> None:
        """Start acquiring continuously, like the Run key."""
        self._send([":RUN"], "run")

    def stop(self) -> None:
        """Stop acquiring, like the Stop key. The screen keeps the last capture."""
        self._send([":STOP"], "stop")

    def single(self) -> None:
        """Arm one acquisition, like the Single key. Does not wait for it."""
        self._send([":SINGle"], "single")

    def autoscale(self) -> None:
        """Run the scope's autoscale. Changes most settings."""
        self._send([":AUToscale"], "autoscale")

    # -- waveforms ----------------------------------------------------------

    def read_screen(
        self,
        analog: Iterable[int] | None = None,
        digital: Iterable[int] | None = None,
        *,
        points: int | None = None,
        settings: Settings | None = None,
    ) -> Capture:
        """Read the waveforms the scope is displaying, changing nothing.

        No re-arm, no run/stop, no setting touched: only queries, plus the
        waveform transfer's source, format and point-count selection, which
        are not visible on the scope.

        If the scope is **running**, each channel is its most recent
        acquisition at the moment that channel is read, and a transfer takes
        about a second per channel at 19200 baud --- so two channels may come
        from different triggers. Stop the scope first for a picture where
        everything is from the same trigger.

        Args:
            analog: Analog channels to read. Default: every one displayed.
            digital: Logic channels to read. Default: every one displayed.
            points: Analog record length, one of
                :data:`~hp54645d.settings.NORMAL_POINTS`; each spans the whole
                screen. Default: the scope's normal record, 2000 at most
                timebases. Fewer is faster: 500 points transfer in about 0.45 s
                against 1.2 s for 2000. Digital pods always come at their own
                length.
            settings: Settings from an earlier :meth:`read_settings`, to skip
                reading them again (about a second) --- for repeated readouts
                such as a live view. They are trusted: a channel hidden on the
                front panel since then is not caught up front, and its
                transfer times out instead.

        Raises:
            ScopeError: If a requested channel is not displayed (the scope
                has nothing to send for it), nothing is displayed at all, or
                the timebase is not in MAIN mode.
            ValueError: If ``points`` is not a length the scope offers.
        """
        _check_points(points)
        settings = settings or self.read_settings()
        _require_main_timebase(settings)
        analog, digital = self._displayed(analog, digital, settings)
        return self._read(analog, digital, settings, origin="screen", points=points)

    def acquire(
        self,
        analog: Iterable[int] = (1,),
        digital: Iterable[int] = (),
        *,
        timeout_s: float = 10.0,
        points: int | None = None,
    ) -> Capture:
        """Take a new triggered capture and read it.

        Switches on any requested channel that is hidden (the scope only
        captures what it displays), arms a single acquisition, waits for the
        trigger, then reads every requested channel --- all from the same
        trigger. Leaves the scope stopped, showing that capture.

        Deliberately not `:DIGitize`: that blocks the instrument's command
        parser until a trigger arrives, and over RS-232 nothing can abort it
        --- only a front-panel key does. A single acquisition leaves the
        parser free, so this can give up and stop.

        Args:
            analog: Analog channels to capture.
            digital: Logic channels to capture (54645D only).
            timeout_s: How long to wait for a trigger.
            points: Analog record length; see :meth:`read_screen`.

        Raises:
            TriggerTimeout: If nothing triggered in time. The acquisition is
                stopped first, so the scope is usable again.
            ScopeError: If the timebase is not in MAIN mode.
        """
        _check_points(points)
        analog, digital = tuple(analog), tuple(digital)
        for n in analog:
            _check_member(n, ANALOG_CHANNELS, "analog channel")
        for n in digital:
            _check_member(n, DIGITAL_CHANNELS, "digital channel")
        if digital:
            self._require_digital()
        if not analog and not digital:
            raise ValueError("nothing to acquire: give analog and/or digital channels")

        settings = self.read_settings()
        _require_main_timebase(settings)
        show = [f":VIEW ANALog{n}" for n in analog if not settings.analog[n].displayed]
        show += [f":VIEW DIGital{n}" for n in digital if n not in settings.digital.displayed]
        self._send(show, "showing channels to acquire")

        self._io.write(":STOP")
        self._io.query(":TER?")  # reading clears it (§6-11), so any 1 from here is ours
        self._io.write(":SINGle")
        deadline = time.monotonic() + timeout_s
        while not parse_bool(self._io.query(":TER?")):
            if time.monotonic() > deadline:
                self._io.write(":STOP")
                self._io.errors()
                raise TriggerTimeout(
                    f"no trigger within {timeout_s:g} s "
                    f"({settings.trigger.source} {settings.trigger.slope.lower()} at "
                    f"{settings.trigger.level_v:g} V, {settings.trigger.mode.lower()} mode); "
                    f"the acquisition was stopped"
                )
            time.sleep(_POLL_S)

        # The trigger is in; the record fills until the right edge of the
        # screen. Wait out the longest that can take.
        time.sleep(max(0.0, settings.timebase.delay_s) + settings.timebase.range_s + _POLL_S)
        self._io.check("single acquisition")

        if show:
            settings = self.read_settings()
        return self._read(analog, digital, settings, origin="acquisition", points=points)

    def memory_sizes(
        self,
        analog: Iterable[int] | None = None,
        digital: Iterable[int] | None = None,
        *,
        settings: Settings | None = None,
    ) -> dict[str, int]:
        """Points a :meth:`read_memory` would transfer, per source. Queries only.

        Call it on a **stopped** scope: the depth depends on how the scope
        stopped. On the bench, at 200 µs/div: 400,000 points while running,
        500,000 after Stop, 1,000,000 after Single --- and 2,000,000 for a
        digital pod after Single.

        Args:
            analog: Analog channels. Default: every one displayed.
            digital: Logic channels; their pods are what is sized. Default:
                every one displayed.
            settings: Settings already read, to skip reading them.

        Returns:
            Source name (``"ANALOG1"``, ``"POD1"``) to record length in points.
            One byte per point, so at 19200 baud a million points is about
            520 s.
        """
        settings = settings or self.read_settings()
        analog, digital = self._displayed(analog, digital, settings)
        sizes = {}
        for source in [f"ANALog{n}" for n in analog] + [f"POD{p}" for p in _pods(digital)]:
            self._select(source, "ALL")
            sizes[source.upper()] = Preamble.parse(self._io.query(":WAVeform:PREamble?")).points
        self._io.check("sizing the acquisition memory")
        return sizes

    def read_memory(
        self,
        analog: Iterable[int] | None = None,
        digital: Iterable[int] | None = None,
        *,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> Capture:
        """Read the scope's whole acquisition memory --- every stored point.

        For a **stopped** scope only: this sends `:STOP` first, so the memory
        cannot change during a transfer that takes minutes. Stopping a scope
        that is already stopped changes nothing (checked on the bench: the
        record is identical before and after).

        The record covers more than the screen and at the full sample rate:
        at 200 µs/div, 5 ms at 5 ns per point after a Single, against the
        screen's 2 ms at 1 µs. See :meth:`memory_sizes` for how long it is
        before committing to the transfer.

        **Slow, and not interruptible.** At 19200 baud a million points take
        about nine minutes, and once a source's transfer has started the scope
        sends all of it.

        Args:
            analog: Analog channels to read. Default: every one displayed.
            digital: Logic channels to read. Default: every one displayed.
                Each pod read is the full pod, eight channels at a time.
            progress: Called as ``progress(source, bytes_read, bytes_total)``
                during each transfer, e.g. ``("ANALOG1", 40960, 1000000)``.

        Raises:
            ScopeError: If a requested channel is not displayed, nothing is
                displayed, or the timebase is not in MAIN mode.
        """
        self._send([":STOP"], "stop before reading the memory")
        settings = self.read_settings()
        _require_main_timebase(settings)
        analog, digital = self._displayed(analog, digital, settings)
        return self._read(analog, digital, settings, origin="memory", points="ALL",
                          progress=progress)

    # -- internals ----------------------------------------------------------

    def _displayed(self, analog, digital, settings: Settings) -> tuple[tuple, tuple]:
        """Resolve the channels to read, and refuse any that are hidden."""
        analog = settings.displayed_analog if analog is None else tuple(analog)
        digital = settings.digital.displayed if digital is None else tuple(digital)
        if digital:
            self._require_digital()
        hidden = [f"ch{n}" for n in analog if not settings.analog[n].displayed]
        hidden += [f"D{n}" for n in digital if n not in settings.digital.displayed]
        if hidden:
            raise ScopeError(
                f"{', '.join(hidden)} not displayed, so the scope has nothing to send "
                f"for them. Display them, or use acquire()."
            )
        if not analog and not digital:
            raise ScopeError("nothing is displayed on the scope")
        return analog, digital

    def _read(
        self,
        analog,
        digital,
        settings: Settings,
        origin: str,
        points: int | str | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> Capture:
        analog_traces: dict[int, AnalogTrace] = {}
        for n in analog:
            preamble, data = self._read_source(f"ANALog{n}", points, progress)
            analog_traces[n] = decode_analog(n, preamble, data)
        digital_traces: dict[int, DigitalTrace] = {}
        for pod in _pods(digital):
            # A pod has one record length for a screen readout; "ALL" applies
            # to it as to an analog source.
            preamble, data = self._read_source(
                f"POD{pod}", "ALL" if points == "ALL" else None, progress
            )
            channels = decode_pod(pod, preamble, data)
            digital_traces.update({n: channels[n] for n in digital if pod_of(n) == pod})
        self._io.check("waveform transfer")
        return Capture(analog_traces, dict(sorted(digital_traces.items())), settings, origin)

    def _select(self, source: str, points: int | str | None = None) -> None:
        """Choose what the next transfer sends (§8-15). Invisible on the scope.

        ``points``: ``None`` for the normal screen record, a count from
        :data:`~hp54645d.settings.NORMAL_POINTS` for a thinner one, or
        ``"ALL"`` for the whole acquisition memory. A count applies to analog
        sources only; pods are read at their own record length, the only one
        checked on the bench.
        """
        if points == "ALL":
            mode = "ALL"
        elif points and source.startswith("ANALog"):
            mode = f"NORMal,{points}"
        else:
            mode = "NORMal"
        self._io.write(f":WAVeform:SOURce {source}")
        self._io.write(":WAVeform:FORMat BYTE")
        self._io.write(f":WAVeform:POINts {mode}")

    def _read_source(
        self,
        source: str,
        points: int | str | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> tuple[Preamble, bytes]:
        """Transfer one source's record: preamble, then the block (§8-15)."""
        self._select(source, points)
        preamble = Preamble.parse(self._io.query(":WAVeform:PREamble?"))
        report = None
        if progress is not None:
            def report(done: int, total: int) -> None:
                progress(source.upper(), done, total)
        data = self._io.query_block(":WAVeform:DATA?", report)
        if len(data) != preamble.points:
            raise ScopeError(
                f"{source}: received {len(data)} points, the preamble announced {preamble.points}"
            )
        return preamble, data

    def _send(self, commands: list[str], context: str) -> None:
        for command in commands:
            self._io.write(command)
        if commands:
            self._io.check(f"{context} ({'; '.join(commands)})")

    def _require_digital(self) -> None:
        if not self.has_digital:
            raise ScopeError(f"the {self.model} has no digital channels; that is the 54645D")


def _require_main_timebase(settings: Settings) -> None:
    if settings.timebase.mode != "MAIN":
        raise ScopeError(
            f"the timebase is in {settings.timebase.mode} mode; waveform readout needs "
            f"MAIN (§2-6)"
        )


def _pods(digital: Iterable[int]) -> list[int]:
    """The pods holding these logic channels, in order."""
    return sorted({pod_of(n) for n in digital})


def _check_points(points: int | None) -> None:
    if points is not None:
        _check_member(points, NORMAL_POINTS, "record length")


def _check_member(value: int, allowed: tuple[int, ...], what: str) -> None:
    if value not in allowed:
        raise ValueError(f"{what} {value!r} does not exist; use one of {allowed}")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {value!r}")
    return f"{value:.6G}"


def _on_off(flag: bool) -> str:
    return "ON" if flag else "OFF"
