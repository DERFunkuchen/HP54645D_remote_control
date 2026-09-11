# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Waveform data: decoding what the scope sends, and holding the result.

Analog records are 8-bit codes scaled by the preamble (§8-15)::

    time    = (index - x_reference) * x_increment + x_origin
    voltage = (code  - y_reference) * y_increment + y_origin

Codes are screen positions: 0–255 bottom to top, so ``y_increment`` is the
channel's range over 256. Times are relative to the trigger.

Digital data comes a **pod** at a time --- eight channels in one byte per
sample: pod 1 is D0–D7, pod 2 is D8–D15. On the 54645D a pod record has
fewer points than an analog one over the same span (500 against 2000 at
2 ms full screen, as read on the bench).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from hp54645d.errors import ScopeError
from hp54645d.settings import Settings, pod_of


@dataclass(frozen=True)
class Preamble:
    """The scaling that comes with a waveform (`:WAVeform:PREamble?`, §8-15)."""

    format: int
    type: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    @classmethod
    def parse(cls, answer: str) -> Preamble:
        """Parse the ten comma-separated fields.

        Raises:
            ScopeError: If there are not ten numeric fields. A short preamble
                decoded anyway would give plausible wrong numbers.
        """
        fields = answer.strip().split(",")
        if len(fields) != 10:
            raise ScopeError(f"waveform preamble has {len(fields)} fields, expected 10: {answer!r}")
        try:
            numbers = [float(f) for f in fields]
        except ValueError as exc:
            raise ScopeError(f"waveform preamble is not numeric: {answer!r}") from exc
        fmt, typ, points, count = (int(n) for n in numbers[:4])
        return cls(fmt, typ, points, count, *numbers[4:])

    def times(self, count: int) -> np.ndarray:
        """Return the sample times, in seconds relative to the trigger."""
        return (np.arange(count) - self.x_reference) * self.x_increment + self.x_origin

    def volts(self, codes: np.ndarray) -> np.ndarray:
        """Return analog codes converted to volts."""
        return (codes.astype(float) - self.y_reference) * self.y_increment + self.y_origin


@dataclass
class AnalogTrace:
    """One analog channel's record."""

    channel: int
    time_s: np.ndarray
    volts: np.ndarray
    preamble: Preamble


@dataclass
class DigitalTrace:
    """One logic channel's record: ``True`` for high."""

    channel: int
    time_s: np.ndarray
    levels: np.ndarray


def decode_analog(channel: int, preamble: Preamble, data: bytes) -> AnalogTrace:
    """Turn an analog BYTE record into times and volts."""
    codes = np.frombuffer(data, dtype=np.uint8)
    return AnalogTrace(channel, preamble.times(len(codes)), preamble.volts(codes), preamble)


def decode_pod(pod: int, preamble: Preamble, data: bytes) -> dict[int, DigitalTrace]:
    """Split a pod record into its eight channels.

    Bit *n* of each byte is channel ``8 * (pod - 1) + n`` --- the obvious
    mapping, and the only one consistent with pod 1 being D0–D7, but not yet
    confirmed against a known signal on the bench.
    """
    codes = np.frombuffer(data, dtype=np.uint8)
    times = preamble.times(len(codes))
    first = 8 * (pod - 1)
    return {
        first + bit: DigitalTrace(first + bit, times, ((codes >> bit) & 1).astype(bool))
        for bit in range(8)
    }


@dataclass
class Capture:
    """Waveforms read from the scope, with the settings they were taken under.

    Attributes:
        analog: Analog traces by channel number (1, 2).
        digital: Logic traces by channel number (0–15).
        settings: The scope's settings when the data was read.
        origin: ``"screen"`` for a readout of what was displayed,
            ``"acquisition"`` for a freshly triggered capture, ``"memory"``
            for the whole acquisition memory of a stopped scope.
        timestamp: When the data was read.
        file: The file it was loaded from, if it was; see :func:`load_capture`.
    """

    analog: dict[int, AnalogTrace]
    digital: dict[int, DigitalTrace]
    settings: Settings
    origin: str
    timestamp: datetime = field(default_factory=datetime.now)
    file: Path | None = None

    @property
    def points(self) -> int:
        """The longest record in the capture."""
        traces = list(self.analog.values()) + list(self.digital.values())
        return max((len(t.time_s) for t in traces), default=0)

    def save(self, path: str | Path) -> list[Path]:
        """Write the capture, as CSV or NumPy ``.npz`` by the file's extension.

        Returns:
            The files written.
        """
        path = Path(path)
        if path.suffix.lower() == ".npz":
            return [self.save_npz(path)]
        return self.save_csv(path)

    def save_csv(self, path: str | Path) -> list[Path]:
        """Write the traces as CSV.

        Analog channels go to ``path``, one column per channel against a
        shared time column. Digital channels, which have their own time axis,
        go beside it as ``<name>_digital.csv``, as 0/1 columns. Should analog
        channels ever differ in record length, each gets its own file,
        ``<name>_ch1.csv`` and so on.

        A million points make a file of about 25 MB; see :meth:`save_npz` for
        something smaller and faster to load.

        The header comments carry the origin and time, each channel's scale in
        words, and the full settings as one JSON line, so :func:`load_capture`
        can rebuild the capture exactly.

        Returns:
            The files written.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = [f"HP 54645D {self.origin}, {self.timestamp.isoformat(timespec='seconds')}",
                  f"settings: {json.dumps(asdict(self.settings))}"]
        written = []

        analog = list(self.analog.values())
        groups = [analog] if _same_axis(analog) else [[t] for t in analog]
        for traces in groups if analog else []:
            target = path if len(groups) == 1 else path.with_name(
                f"{path.stem}_ch{traces[0].channel}{path.suffix or '.csv'}")
            scales = [f"ch{t.channel}: {self.settings.analog[t.channel].range_v:g} V full "
                      f"screen, offset {self.settings.analog[t.channel].offset_v:g} V"
                      for t in traces]
            _write_columns(target, header + scales,
                           ["time_s"] + [f"ch{t.channel}_v" for t in traces],
                           [traces[0].time_s] + [t.volts for t in traces],
                           ["%.10g"] + ["%.6g"] * len(traces))
            written.append(target)

        if self.digital:
            traces = list(self.digital.values())
            digital_path = path.with_name(f"{path.stem}_digital{path.suffix or '.csv'}")
            _write_columns(digital_path, header,
                           ["time_s"] + [f"d{t.channel}" for t in traces],
                           [traces[0].time_s] + [t.levels.astype(np.uint8) for t in traces],
                           ["%.10g"] + ["%d"] * len(traces))
            written.append(digital_path)
        return written

    def save_npz(self, path: str | Path) -> Path:
        """Write the capture as a compressed NumPy archive.

        Far smaller and faster than CSV for a full-memory record, and loads
        with ``numpy.load``. Arrays: ``ch1_time_s``, ``ch1_v`` per analog
        channel; ``pod1_time_s`` and ``d0`` … per logic channel (0/1); and
        ``meta``, a JSON string with the settings, origin and timestamp.

        Returns:
            The file written.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays: dict[str, np.ndarray] = {}
        for n, trace in self.analog.items():
            arrays[f"ch{n}_time_s"] = trace.time_s
            arrays[f"ch{n}_v"] = trace.volts
        for n, trace in self.digital.items():
            arrays.setdefault(f"pod{pod_of(n)}_time_s", trace.time_s)
            arrays[f"d{n}"] = trace.levels.astype(np.uint8)
        meta = {"instrument": "HP 54645D", "origin": self.origin,
                "timestamp": self.timestamp.isoformat(), "settings": asdict(self.settings)}
        arrays["meta"] = np.array(json.dumps(meta))
        np.savez_compressed(path, **arrays)
        return path


def load_capture(path: str | Path) -> Capture:
    """Read a capture saved by :meth:`Capture.save` back in.

    ``.npz`` files come back exactly, settings included. For CSV, give any of
    the files one save wrote --- ``run.csv``, ``run_digital.csv`` or
    ``run_ch1.csv`` --- and the others beside it are loaded too. Settings come
    from the header's JSON line; CSV files written before that line existed
    fall back to the per-channel scale comments, and the timebase is then
    taken to be the whole record.

    Returns:
        The capture, with :attr:`Capture.file` set to ``path``.

    Raises:
        ScopeError: If the file is not something this package wrote.
    """
    path = Path(path)
    if not path.exists():
        raise ScopeError(f"no such file: {path}")
    if path.suffix.lower() == ".npz":
        capture = _load_npz(path)
    else:
        capture = _load_csv(path)
    capture.file = path
    return capture


def _load_npz(path: Path) -> Capture:
    with np.load(path) as archive:
        if "meta" not in archive:
            raise ScopeError(f"{path.name} is not an HP 54645D capture (no 'meta' entry)")
        meta = json.loads(str(archive["meta"]))
        analog, digital = {}, {}
        for name in archive.files:
            if name.startswith("ch") and name.endswith("_v"):
                n = int(name[2:-2])
                times, volts = archive[f"ch{n}_time_s"], archive[name]
                analog[n] = AnalogTrace(n, times, volts, _preamble_of(times))
            elif name.startswith("d") and name[1:].isdigit():
                n = int(name[1:])
                digital[n] = DigitalTrace(n, archive[f"pod{pod_of(n)}_time_s"],
                                          archive[name].astype(bool))
    return Capture(dict(sorted(analog.items())), dict(sorted(digital.items())),
                   Settings.from_dict(meta.get("settings") or {}),
                   meta.get("origin", "file"), _timestamp(meta.get("timestamp")))


def _load_csv(path: Path) -> Capture:
    base = path.stem
    for suffix in ("_digital", "_ch1", "_ch2"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    folder, ext = path.parent, path.suffix or ".csv"
    candidates = [folder / f"{base}{ext}", folder / f"{base}_ch1{ext}",
                  folder / f"{base}_ch2{ext}", folder / f"{base}_digital{ext}"]
    files = [f for f in candidates if f.exists()]
    if not files:
        raise ScopeError(f"no capture files found for {path}")

    analog, digital = {}, {}
    settings, origin, timestamp, scales = None, "file", datetime.now(), {}
    for file in files:
        comments, names, table = _read_csv(file)
        for line in comments:
            if line.startswith("HP 54645D "):
                origin, _, stamp = line[len("HP 54645D "):].partition(", ")
                timestamp = _timestamp(stamp)
            elif line.startswith("settings: "):
                settings = Settings.from_dict(json.loads(line[len("settings: "):]))
            elif line.startswith("ch") and "full screen" in line:
                n = int(line[2:line.index(":")])
                words = line.replace(",", "").split()
                scales[n] = (float(words[1]), float(words[words.index("offset") + 1]))
        times = table[:, 0]
        for column, name in enumerate(names[1:], start=1):
            if name.startswith("ch") and name.endswith("_v"):
                n = int(name[2:-2])
                analog[n] = AnalogTrace(n, times, table[:, column], _preamble_of(times))
            elif name.startswith("d") and name[1:].isdigit():
                n = int(name[1:])
                digital[n] = DigitalTrace(n, times, table[:, column] > 0.5)

    if settings is None:  # written before the settings line existed
        settings = Settings()
        for n, (range_v, offset_v) in scales.items():
            settings.analog[n].range_v, settings.analog[n].offset_v = range_v, offset_v
        traces = list(analog.values()) + list(digital.values())
        settings.timebase.range_s = float(np.ptp(traces[0].time_s)) if traces else 1e-3
        settings.digital.displayed = tuple(sorted(digital))
    for n in analog:
        settings.analog[n].displayed = True
    return Capture(dict(sorted(analog.items())), dict(sorted(digital.items())), settings,
                   origin, timestamp)


def _read_csv(file: Path) -> tuple[list[str], list[str], np.ndarray]:
    """Header comments (without ``# ``), column names, and the numbers."""
    comments: list[str] = []
    with file.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                comments.append(line[1:].strip())
                continue
            names = [n.strip() for n in line.strip().split(",")]
            break
        else:
            raise ScopeError(f"{file.name} has no column names")
    if not names or names[0] != "time_s":
        raise ScopeError(f"{file.name} is not an HP 54645D capture (first column {names[0]!r})")
    table = np.loadtxt(file, delimiter=",", skiprows=len(comments) + 1, ndmin=2,
                       encoding="utf-8")
    return comments, names, table


def _preamble_of(times: np.ndarray) -> Preamble:
    """A preamble for a trace loaded from a file: the time axis is known, the codes are not."""
    step = float(times[1] - times[0]) if len(times) > 1 else 0.0
    nan = float("nan")
    return Preamble(0, 0, len(times), 1, step, float(times[0]) if len(times) else 0.0, 0,
                    nan, nan, nan)


def _timestamp(text: str | None) -> datetime:
    try:
        return datetime.fromisoformat(text) if text else datetime.now()
    except ValueError:
        return datetime.now()


def _same_axis(traces: list[AnalogTrace]) -> bool:
    first = traces[0].time_s if traces else None
    return all(len(t.time_s) == len(first) and t.time_s[0] == first[0]
               and t.time_s[-1] == first[-1] for t in traces)


def _write_columns(path: Path, comments: list[str], names: list[str], columns: list,
                   formats: list[str]) -> None:
    lengths = {len(c) for c in columns}
    if len(lengths) != 1:
        raise ScopeError(f"cannot write {path.name}: columns differ in length {sorted(lengths)}")
    table = np.column_stack([np.asarray(c, dtype=float) for c in columns])
    header = "\n".join([f"# {line}" for line in comments] + [",".join(names)])
    np.savetxt(path, table, delimiter=",", fmt=formats, header=header, comments="",
               encoding="utf-8")
