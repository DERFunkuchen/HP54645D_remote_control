# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""The scope's settings, as plain data, and the vocabulary for them.

Choices are named by the long form of the instrument's own mnemonic,
upper-cased: ``"CENTER"`` for ``CENTer``, ``"AUTLEVEL"`` for ``AUTLevel``. The
scope answers queries in the short form (``CENT``, ``AUTL``); :class:`Choices`
translates both ways, so nothing above this module sees a short form.

Spans are **full screen**, as the instrument has them: a range of 16 V is
2 V/div over 8 divisions; a timebase range of 2 ms is 200 µs/div over 10.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field

#: Divisions across the screen, for converting full-screen spans to per-div.
VERTICAL_DIVISIONS = 8
HORIZONTAL_DIVISIONS = 10

ANALOG_CHANNELS = (1, 2)
DIGITAL_CHANNELS = tuple(range(16))
PODS = (1, 2)


def _short(mnemonic: str) -> str:
    """Return the short form of a mnemonic: its upper-case letters and digits."""
    return "".join(ch for ch in mnemonic if ch.isupper() or ch.isdigit())


class Choices:
    """One enumerated setting: the values it takes and how to spell them.

    Built from the manual's mnemonics, e.g. ``Choices("LEFT", "CENTer",
    "RIGHt")``. Accepts any of long form, short form or canonical name, in
    any case.
    """

    def __init__(self, *mnemonics: str) -> None:
        """Take the mnemonics exactly as the manual prints them."""
        self._mnemonic = {m.upper(): m for m in mnemonics}
        self._lookup = {m.upper(): m.upper() for m in mnemonics}
        self._lookup.update({_short(m): m.upper() for m in mnemonics})

    def __iter__(self):
        """Iterate over the canonical names, in manual order."""
        return iter(self._mnemonic)

    def canonical(self, value: str) -> str:
        """Return the canonical name for any accepted spelling.

        Raises:
            ValueError: If ``value`` is not one of the choices.
        """
        try:
            return self._lookup[str(value).strip().upper()]
        except KeyError:
            raise ValueError(f"{value!r} is not one of {list(self)}") from None

    def command(self, value: str) -> str:
        """Return the mnemonic to send for ``value``."""
        return self._mnemonic[self.canonical(value)]


COUPLING = Choices("AC", "DC", "GND")
PROBE = Choices("X1", "X10", "X20", "X100")
TIMEBASE_MODE = Choices("MAIN", "DELayed", "XY", "ROLL")
REFERENCE = Choices("LEFT", "CENTer", "RIGHt")
TRIGGER_MODE = Choices("AUTLevel", "AUTO", "NORMal")
TRIGGER_TYPE = Choices("EDGE", "TV", "GLITch", "ADVanced", "PATTern")
TRIGGER_SOURCE = Choices(
    "ANALog1", "ANALog2", *(f"DIGital{n}" for n in DIGITAL_CHANNELS), "LINE"
)
SLOPE = Choices("POSitive", "NEGative")
TRIGGER_COUPLING = Choices("AC", "DC")
REJECT = Choices("OFF", "LF", "HF")
ACQUIRE_TYPE = Choices("NORMal", "AVERage", "PEAK", "REALtime")
THRESHOLD = Choices("CMOS", "ECL", "TTL", "USERdef")

#: Averaging counts the scope accepts (§8-4).
AVERAGE_COUNTS = (4, 8, 16, 32, 64, 128, 256)

#: Analog record lengths a screen readout can ask for (`:WAVeform:POINts
#: NORMal,<n>`, §8-15). Each spans the whole screen; fewer is faster over
#: RS-232 --- at 19200 baud, 500 points took 0.45 s against 1.2 s for 2000.
NORMAL_POINTS = (100, 200, 250, 400, 500, 800, 1000, 2000, 4000)


@dataclass
class AnalogSettings:
    """One analog channel.

    Attributes:
        channel: 1 or 2.
        displayed: Whether the trace is on screen.
        range_v: Full-screen vertical span, in volts at the probe tip.
        offset_v: Voltage at the centre of the screen.
        coupling: ``"AC"``, ``"DC"`` or ``"GND"``.
        probe: Attenuation the scope scales for: ``"X1"``, ``"X10"``, …
        bandwidth_limit: Whether the low-pass filter is in.
        invert: Whether the trace is inverted.
    """

    channel: int
    displayed: bool = False
    range_v: float = 8.0
    offset_v: float = 0.0
    coupling: str = "DC"
    probe: str = "X1"
    bandwidth_limit: bool = False
    invert: bool = False

    @property
    def volts_per_div(self) -> float:
        """Vertical scale, in volts per division."""
        return self.range_v / VERTICAL_DIVISIONS


@dataclass
class TimebaseSettings:
    """The horizontal axis.

    Attributes:
        mode: ``"MAIN"``, ``"DELAYED"``, ``"XY"`` or ``"ROLL"``. Waveform
            readout works in ``"MAIN"`` only.
        range_s: Full-screen time span, in seconds.
        delay_s: Time from the trigger to the display reference.
        reference: Where the reference sits: ``"LEFT"``, ``"CENTER"``,
            ``"RIGHT"``.
    """

    mode: str = "MAIN"
    range_s: float = 1e-3
    delay_s: float = 0.0
    reference: str = "CENTER"

    @property
    def seconds_per_div(self) -> float:
        """Horizontal scale, in seconds per division."""
        return self.range_s / HORIZONTAL_DIVISIONS


@dataclass
class TriggerSettings:
    """The trigger.

    Attributes:
        mode: ``"AUTLEVEL"``, ``"AUTO"`` or ``"NORMAL"``.
        type: ``"EDGE"``, ``"TV"``, ``"GLITCH"``, ``"ADVANCED"``,
            ``"PATTERN"``. Only the edge trigger's parameters are modelled.
        source: ``"ANALOG1"``, ``"ANALOG2"``, ``"DIGITAL0"`` … ``"DIGITAL15"``
            or ``"LINE"``.
        level_v: Edge trigger level, in volts.
        slope: ``"POSITIVE"`` or ``"NEGATIVE"``.
        coupling: ``"AC"`` or ``"DC"``.
        reject: ``"OFF"``, ``"LF"`` or ``"HF"``.
        holdoff_s: Holdoff, 200 ns to 20 s.
        noise_reject: Whether noise reject is on.
    """

    mode: str = "AUTLEVEL"
    type: str = "EDGE"
    source: str = "ANALOG1"
    level_v: float = 0.0
    slope: str = "POSITIVE"
    coupling: str = "DC"
    reject: str = "OFF"
    holdoff_s: float = 200e-9
    noise_reject: bool = False


@dataclass
class DigitalSettings:
    """The sixteen logic inputs.

    Attributes:
        displayed: Channel numbers (0–15) currently on screen.
        thresholds: Per pod (1 = D0–D7, 2 = D8–D15): ``(kind, volts)``, kind
            being ``"CMOS"``, ``"ECL"``, ``"TTL"`` or ``"USERDEF"``.
    """

    displayed: tuple[int, ...] = ()
    thresholds: dict[int, tuple[str, float]] = field(
        default_factory=lambda: {1: ("TTL", 1.4), 2: ("TTL", 1.4)}
    )


@dataclass
class AcquireSettings:
    """How acquisitions are taken.

    Attributes:
        type: ``"NORMAL"``, ``"AVERAGE"``, ``"PEAK"`` or ``"REALTIME"``.
        count: Averages, when ``type`` is ``"AVERAGE"``.
    """

    type: str = "NORMAL"
    count: int = 1


@dataclass
class Settings:
    """Everything this package reads back from the scope, in one snapshot."""

    analog: dict[int, AnalogSettings] = field(
        default_factory=lambda: {n: AnalogSettings(n) for n in ANALOG_CHANNELS}
    )
    timebase: TimebaseSettings = field(default_factory=TimebaseSettings)
    trigger: TriggerSettings = field(default_factory=TriggerSettings)
    digital: DigitalSettings = field(default_factory=DigitalSettings)
    acquire: AcquireSettings = field(default_factory=AcquireSettings)

    @property
    def displayed_analog(self) -> tuple[int, ...]:
        """Analog channels currently on screen."""
        return tuple(n for n, a in self.analog.items() if a.displayed)

    @classmethod
    def from_dict(cls, data: dict) -> Settings:
        """Rebuild settings from ``dataclasses.asdict`` output, e.g. read from a file.

        Tolerant of what JSON does to it --- string keys, lists for tuples ---
        and of fields it does not know, so a file written by a later version
        still loads. Anything missing keeps its default.
        """
        analog = {n: AnalogSettings(n) for n in ANALOG_CHANNELS}
        for key, fields in (data.get("analog") or {}).items():
            n = int(key)
            analog[n] = AnalogSettings(**_known(AnalogSettings, {**fields, "channel": n}))
        digital_data = data.get("digital") or {}
        digital = DigitalSettings(
            displayed=tuple(int(n) for n in digital_data.get("displayed", ())),
            thresholds={int(pod): (str(kind), float(volts)) for pod, (kind, volts)
                        in (digital_data.get("thresholds") or {}).items()},
        )
        return cls(
            analog=analog,
            timebase=TimebaseSettings(**_known(TimebaseSettings, data.get("timebase") or {})),
            trigger=TriggerSettings(**_known(TriggerSettings, data.get("trigger") or {})),
            digital=digital,
            acquire=AcquireSettings(**_known(AcquireSettings, data.get("acquire") or {})),
        )


def _known(kind: type, fields: dict) -> dict:
    """The entries of ``fields`` that ``kind`` has."""
    names = {f.name for f in dataclasses.fields(kind)}
    return {k: v for k, v in fields.items() if k in names}


@dataclass
class Changes:
    """What differs between two settings snapshots, as ``configure_*`` arguments.

    Built by :func:`changes`; applied by :meth:`hp54645d.HP54645D.apply`.

    Attributes:
        analog: Channel to ``configure_analog`` keyword arguments.
        timebase: ``configure_timebase`` keyword arguments.
        trigger: ``configure_trigger`` keyword arguments.
        digital_displayed: Logic channel to shown/hidden.
        thresholds: Pod to logic family, or ``("USERDEF", volts)``.
        acquire: ``configure_acquire`` keyword arguments.
    """

    analog: dict[int, dict[str, object]] = field(default_factory=dict)
    timebase: dict[str, object] = field(default_factory=dict)
    trigger: dict[str, object] = field(default_factory=dict)
    digital_displayed: dict[int, bool] = field(default_factory=dict)
    thresholds: dict[int, str | tuple[str, float]] = field(default_factory=dict)
    acquire: dict[str, object] = field(default_factory=dict)

    def __bool__(self) -> bool:
        """Whether anything differs."""
        return any((self.analog, self.timebase, self.trigger, self.digital_displayed,
                    self.thresholds, self.acquire))

    @property
    def count(self) -> int:
        """How many individual settings differ."""
        return (sum(len(c) for c in self.analog.values()) + len(self.timebase)
                + len(self.trigger) + len(self.digital_displayed) + len(self.thresholds)
                + len(self.acquire))


# Each section's fields, with the vocabulary for those that are choices.
_ANALOG_FIELDS = {"displayed": None, "range_v": None, "offset_v": None, "coupling": COUPLING,
                  "probe": PROBE, "bandwidth_limit": None, "invert": None}
_TIMEBASE_FIELDS = {"mode": TIMEBASE_MODE, "range_s": None, "delay_s": None,
                    "reference": REFERENCE}
_TRIGGER_FIELDS = {"mode": TRIGGER_MODE, "type": TRIGGER_TYPE, "source": TRIGGER_SOURCE,
                   "level_v": None, "slope": SLOPE, "coupling": TRIGGER_COUPLING,
                   "reject": REJECT, "holdoff_s": None, "noise_reject": None}


def changes(current: Settings, wanted: Settings) -> Changes:
    """Work out what must be sent to turn ``current`` into ``wanted``.

    Pure: no instrument involved. Numbers count as equal within 1 part in
    10⁴, because the scope answers in six significant figures and a value it
    has rounded must not look like a change. A user-defined threshold voltage
    is compared only when the logic family is ``USERDEF``; for the others the
    voltage follows from the family. An average count is included only when
    it is one the scope accepts.
    """
    result = Changes()
    for n, now in current.analog.items():
        if n in wanted.analog:
            differing = _differing(now, wanted.analog[n], _ANALOG_FIELDS)
            if differing:
                result.analog[n] = differing
    result.timebase = _differing(current.timebase, wanted.timebase, _TIMEBASE_FIELDS)
    result.trigger = _differing(current.trigger, wanted.trigger, _TRIGGER_FIELDS)

    shown_now, shown_wanted = set(current.digital.displayed), set(wanted.digital.displayed)
    result.digital_displayed = {n: n in shown_wanted for n in sorted(shown_now ^ shown_wanted)}
    for pod, (kind, volts) in wanted.digital.thresholds.items():
        now_kind, now_volts = current.digital.thresholds.get(pod, (None, float("nan")))
        kind = THRESHOLD.canonical(kind)
        if kind != now_kind or (kind == "USERDEF" and not same_number(volts, now_volts)):
            result.thresholds[pod] = (kind, volts) if kind == "USERDEF" else kind

    if ACQUIRE_TYPE.canonical(wanted.acquire.type) != current.acquire.type:
        result.acquire["type"] = wanted.acquire.type
    if wanted.acquire.count != current.acquire.count and wanted.acquire.count in AVERAGE_COUNTS:
        result.acquire["count"] = wanted.acquire.count
    return result


def same_number(a: float, b: float) -> bool:
    """Equal within the scope's own precision; two NaNs count as equal."""
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return math.isclose(a, b, rel_tol=1e-4, abs_tol=1e-12)


def _differing(now: object, wanted: object, fields: dict[str, Choices | None]
               ) -> dict[str, object]:
    """Fields of ``wanted`` that differ from ``now``; choices compared canonically.

    Raises:
        ValueError: For a choice that does not exist --- before anything is sent.
    """
    found: dict[str, object] = {}
    for name, choices in fields.items():
        old, new = getattr(now, name), getattr(wanted, name)
        if choices is not None:
            new = choices.canonical(new)
            if new != old:
                found[name] = new
        elif isinstance(new, bool) or isinstance(old, bool):
            if bool(new) != bool(old):
                found[name] = bool(new)
        elif not same_number(float(new), float(old)):
            found[name] = float(new)
    return found


def pod_of(digital_channel: int) -> int:
    """Return the pod (1 or 2) a digital channel belongs to."""
    return 1 if digital_channel < 8 else 2


def parse_bool(answer: str) -> bool:
    """Parse ``ON``/``OFF`` or ``1``/``0``."""
    text = answer.strip().upper()
    if text in ("ON", "1", "+1"):
        return True
    if text in ("OFF", "0", "+0"):
        return False
    raise ValueError(f"not a boolean answer: {answer!r}")
