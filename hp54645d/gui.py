# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""A small window for driving the scope: settings, run control, screenshot, live view.

A client of the library and nothing more: every decision about the instrument
--- what to send when settings change, how a live view keeps up, how the full
memory is read --- is made by :mod:`hp54645d`, so a script can do exactly what
the window does. This module holds the form, the buttons, a worker thread and
the dialogs.

tkinter and matplotlib only --- nothing else to install. Every instrument
exchange runs on the worker thread, one at a time, so the window stays
responsive during the second or so a waveform transfer takes at 19200 baud.
Clicks made while an exchange is running are queued and run next.

Start with ``hp54645d-gui`` or ``python -m hp54645d``.
"""

from __future__ import annotations

import copy
import math
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure

from hp54645d.analysis import Measurement, measure
from hp54645d.errors import ApplyError, ScopeError
from hp54645d.live import LiveReader
from hp54645d.plot import SCREEN, plot_capture, time_axis
from hp54645d.scope import HP54645D
from hp54645d.settings import (
    ACQUIRE_TYPE,
    ANALOG_CHANNELS,
    AVERAGE_COUNTS,
    COUPLING,
    DIGITAL_CHANNELS,
    HORIZONTAL_DIVISIONS,
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
    VERTICAL_DIVISIONS,
    Settings,
    changes,
    pod_of,
)
from hp54645d.transport import BAUD_RATES, FLOW_CONTROLS
from hp54645d.waveform import load_capture

_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6}

#: Record lengths offered in the window; fewer is faster over RS-232.
POINT_CHOICES = ("2000", "1000", "500", "250", "100")

_DATA_FILES = [("NumPy archive", "*.npz"), ("CSV", "*.csv")]


def parse_si(text: str) -> float:
    """Parse a number with an optional SI suffix: ``"200u"`` -> 0.0002.

    Raises:
        ValueError: If it is not a number.
    """
    text = text.strip().replace(" ", "")
    for suffix in ("s", "V"):  # tolerate a unit: "200us", "2V"
        if len(text) > 1 and text.endswith(suffix):
            text = text[:-1]
    if text and text[-1] in _SI:
        return float(text[:-1]) * _SI[text[-1]]
    return float(text)


def format_si(value: float) -> str:
    """Format for an entry field: 0.0002 -> ``"200u"``.

    Six significant figures, as the scope answers: fewer would make a value
    read back from the scope look changed on the next *Apply*.
    """
    if value == 0 or not math.isfinite(value):
        return f"{value:g}"
    for suffix, factor in (("", 1.0), ("m", 1e-3), ("u", 1e-6), ("n", 1e-9)):
        if abs(value) >= factor * 0.9999:
            return f"{value / factor:.6g}{suffix}"
    return f"{value / 1e-12:.6g}p"


_MEASURE_COLUMNS = ("trace", "min", "max", "pk-pk", "mean", "rms", "frequency", "period",
                    "duty", "high")


def format_measurement(measurement: Measurement) -> str:
    """``Measurement("frequency", 1232.7, "Hz")`` -> ``"1.2327 kHz"``."""
    if measurement.unit == "%":
        return f"{measurement.value:.1f} %"
    value = measurement.value
    for factor, prefix in ((1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"),
                           (1e-9, "n")):
        if abs(value) >= factor:
            return f"{value / factor:.5g} {prefix}{measurement.unit}"
    return f"{value:.3g} {measurement.unit}"


def transfer_seconds(points: int, baud_rate: int) -> float:
    """How long a binary record takes on the line: one byte per point, 10 bits a byte."""
    return points * 10 / baud_rate


def format_duration(seconds: float) -> str:
    """``75`` -> ``"1 min 15 s"``."""
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest} s" if minutes else f"{rest} s"


class App:
    """The main window."""

    def __init__(self, root: tk.Tk) -> None:
        """Build the window. Nothing is connected until *Connect*."""
        self.root = root
        self.scope: HP54645D | None = None
        self.settings: Settings | None = None
        self.capture = None
        self._baud = 19200
        self._results: queue.Queue = queue.Queue()
        self._pending: list[tuple] = []
        self._busy = False
        self._connecting = False
        self._buttons: list[ttk.Button] = []
        self._vars: dict[tuple, tk.Variable] = {}
        self._live: LiveReader | None = None
        self._progress: tuple[str, int, int, float] | None = None

        root.title("HP 54645D remote control")
        root.geometry("1280x840")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_connection_bar()
        body = ttk.PanedWindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=6, pady=(0, 4))
        controls = ttk.Frame(body, width=360)
        plot = ttk.Frame(body)
        body.add(controls, weight=0)
        body.add(plot, weight=1)
        self._build_controls(controls)
        self._build_plot(plot)

        self.status = tk.StringVar(value="Not connected.")
        ttk.Label(root, textvariable=self.status, anchor="w", relief="sunken",
                  padding=(6, 2)).pack(fill="x", side="bottom")
        self._set_connected(False)
        root.after(50, self._poll)

    # -- layout -------------------------------------------------------------

    def _build_connection_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(fill="x")
        self.address = tk.StringVar(value="ASRL5::INSTR")
        self.baud = tk.StringVar(value="19200")
        self.flow = tk.StringVar(value="xon_xoff")
        ttk.Label(bar, text="Address").pack(side="left")
        ttk.Entry(bar, textvariable=self.address, width=18).pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="Baud").pack(side="left")
        ttk.Combobox(bar, textvariable=self.baud, values=BAUD_RATES, width=7,
                     state="readonly").pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="Handshake").pack(side="left")
        ttk.Combobox(bar, textvariable=self.flow, values=FLOW_CONTROLS, width=9,
                     state="readonly").pack(side="left", padx=(4, 10))
        self.connect_button = ttk.Button(bar, text="Connect", command=self._toggle_connection)
        self.connect_button.pack(side="left")
        self.identity = tk.StringVar()
        ttk.Label(bar, textvariable=self.identity, foreground="#555").pack(side="left", padx=12)

    def _build_controls(self, parent: ttk.Frame) -> None:
        tabs = ttk.Notebook(parent)
        tabs.pack(fill="x")
        self._build_channels_tab(tabs)
        self._build_timebase_tab(tabs)
        self._build_trigger_tab(tabs)
        self._build_digital_tab(tabs)
        self._build_acquire_tab(tabs)

        row = ttk.Frame(parent, padding=(0, 6))
        row.pack(fill="x")
        self._button(row, "Read from scope", self._refresh).pack(side="left", expand=True, fill="x")
        self._button(row, "Apply changes", self._apply).pack(side="left", expand=True, fill="x")

        run = ttk.LabelFrame(parent, text="Run control", padding=6)
        run.pack(fill="x", pady=4)
        for label, method in (("Run", "run"), ("Stop", "stop"), ("Single", "single"),
                              ("Autoscale", "autoscale")):
            self._button(run, label, lambda m=method: self._run_control(m)).pack(
                side="left", expand=True, fill="x")

        data = ttk.LabelFrame(parent, text="Waveforms", padding=6)
        data.pack(fill="x", pady=4)

        read_row = ttk.Frame(data)
        read_row.pack(fill="x")
        self._button(read_row, "Screenshot", self._screenshot).pack(
            side="left", expand=True, fill="x")
        self.live_var = tk.BooleanVar(value=False)
        self.live_toggle = ttk.Checkbutton(read_row, text="● Live", variable=self.live_var,
                                           command=self._toggle_live, style="Toolbutton")
        self.live_toggle.pack(side="left", expand=True, fill="x", padx=(4, 0))

        points_row = ttk.Frame(data)
        points_row.pack(fill="x", pady=(4, 0))
        ttk.Label(points_row, text="Points per channel").pack(side="left")
        self.points = tk.StringVar(value="1000")
        ttk.Combobox(points_row, textvariable=self.points, values=POINT_CHOICES, width=6,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(points_row, text="fewer = faster live view", foreground="#666").pack(
            side="left")

        acquire_row = ttk.Frame(data)
        acquire_row.pack(fill="x", pady=(4, 0))
        self._button(acquire_row, "Acquire (single, displayed channels)",
                     self._acquire).pack(side="left", expand=True, fill="x")
        ttk.Label(acquire_row, text="timeout s").pack(side="left", padx=(6, 2))
        self.acquire_timeout = tk.StringVar(value="10")
        ttk.Entry(acquire_row, textvariable=self.acquire_timeout, width=5).pack(side="left")

        self._button(data, "Full memory… (stops the scope, reads every point)",
                     self._full_memory).pack(fill="x", pady=(4, 0))

        view_row = ttk.Frame(data)
        view_row.pack(fill="x", pady=(4, 0))
        ttk.Label(view_row, text="View").pack(side="left")
        self.layout = tk.StringVar(value="split")
        for text, value in (("Separate panels", "split"), ("One screen", "overlay")):
            ttk.Radiobutton(view_row, text=text, value=value, variable=self.layout,
                            command=self._redraw).pack(side="left", padx=(6, 0))

        save_row = ttk.Frame(data)
        save_row.pack(fill="x", pady=(4, 0))
        self._button(save_row, "Open file…", self._open_file, needs_scope=False).pack(
            side="left", expand=True, fill="x")
        self._button(save_row, "Save data…", self._save_data, needs_scope=False).pack(
            side="left", expand=True, fill="x")
        self._button(save_row, "Save PNG…", self._save_png, needs_scope=False).pack(
            side="left", expand=True, fill="x")

        console = ttk.LabelFrame(parent, text="Command (queries end in ?)", padding=6)
        console.pack(fill="x", pady=4)
        self.command = tk.StringVar(value=":ANALog1:RANGe?")
        entry = ttk.Entry(console, textvariable=self.command)
        entry.pack(side="left", expand=True, fill="x")
        entry.bind("<Return>", lambda _event: self._send_command())
        self._button(console, "Send", self._send_command).pack(side="left", padx=(4, 0))

    def _build_channels_tab(self, tabs: ttk.Notebook) -> None:
        tab = ttk.Frame(tabs, padding=6)
        tabs.add(tab, text="Channels")
        for n in ANALOG_CHANNELS:
            box = ttk.LabelFrame(tab, text=f"Channel {n}", padding=6)
            box.pack(fill="x", pady=3)
            self._check(box, ("analog", n, "displayed"), "Display", 0, 0)
            self._check(box, ("analog", n, "bandwidth_limit"), "BW limit", 0, 1)
            self._check(box, ("analog", n, "invert"), "Invert", 0, 2)
            self._entry(box, ("analog", n, "volts_per_div"), "V/div", 1)
            self._entry(box, ("analog", n, "offset_v"), "Offset V", 2)
            self._combo(box, ("analog", n, "coupling"), "Coupling", COUPLING, 3)
            self._combo(box, ("analog", n, "probe"), "Probe", PROBE, 4)

    def _build_timebase_tab(self, tabs: ttk.Notebook) -> None:
        tab = ttk.Frame(tabs, padding=6)
        tabs.add(tab, text="Timebase")
        self._combo(tab, ("timebase", "mode"), "Mode", TIMEBASE_MODE, 0)
        self._entry(tab, ("timebase", "seconds_per_div"), "Time/div (s)", 1)
        self._entry(tab, ("timebase", "delay_s"), "Delay (s)", 2)
        self._combo(tab, ("timebase", "reference"), "Reference", REFERENCE, 3)
        ttk.Label(tab, text="Numbers take SI suffixes: 200u, 2m, 1.5k", foreground="#666").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_trigger_tab(self, tabs: ttk.Notebook) -> None:
        tab = ttk.Frame(tabs, padding=6)
        tabs.add(tab, text="Trigger")
        self._combo(tab, ("trigger", "mode"), "Mode", TRIGGER_MODE, 0)
        self._combo(tab, ("trigger", "type"), "Type", TRIGGER_TYPE, 1)
        self._combo(tab, ("trigger", "source"), "Source", TRIGGER_SOURCE, 2)
        self._entry(tab, ("trigger", "level_v"), "Level V", 3)
        self._combo(tab, ("trigger", "slope"), "Slope", SLOPE, 4)
        self._combo(tab, ("trigger", "coupling"), "Coupling", TRIGGER_COUPLING, 5)
        self._combo(tab, ("trigger", "reject"), "Reject", REJECT, 6)
        self._entry(tab, ("trigger", "holdoff_s"), "Holdoff (s)", 7)
        self._check(tab, ("trigger", "noise_reject"), "Noise reject", 8, 0)

    def _build_digital_tab(self, tabs: ttk.Notebook) -> None:
        tab = ttk.Frame(tabs, padding=6)
        tabs.add(tab, text="Digital")
        shown = ttk.LabelFrame(tab, text="Displayed", padding=6)
        shown.pack(fill="x")
        for n in DIGITAL_CHANNELS:
            self._check(shown, ("digital", n), f"D{n}", n // 8, n % 8)
        for pod in PODS:
            box = ttk.LabelFrame(tab, text=f"Pod {pod} threshold (D{8 * pod - 8}–D{8 * pod - 1})",
                                 padding=6)
            box.pack(fill="x", pady=3)
            self._combo(box, ("threshold", pod, "kind"), "Logic", THRESHOLD, 0)
            self._entry(box, ("threshold", pod, "volts"), "User V", 1)

    def _build_acquire_tab(self, tabs: ttk.Notebook) -> None:
        tab = ttk.Frame(tabs, padding=6)
        tabs.add(tab, text="Acquisition")
        self._combo(tab, ("acquire", "type"), "Type", ACQUIRE_TYPE, 0)
        self._combo(tab, ("acquire", "count"), "Averages", [str(c) for c in AVERAGE_COUNTS], 1)

    def _build_plot(self, parent: ttk.Frame) -> None:
        self.figure = Figure(figsize=(8, 6), facecolor=SCREEN)
        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)

        table = ttk.LabelFrame(parent, text="Measurements", padding=(6, 2))
        table.pack(side="bottom", fill="x")
        self.window_label = tk.StringVar(value="Over the visible time range; zoom to narrow it.")
        ttk.Label(table, textvariable=self.window_label, foreground="#555").pack(anchor="w")
        self.measurements = ttk.Treeview(table, columns=_MEASURE_COLUMNS, show="headings",
                                         height=4)
        for column in _MEASURE_COLUMNS:
            self.measurements.heading(column, text=column)
            self.measurements.column(column, width=90, anchor="e", stretch=True)
        self.measurements.column("trace", width=60, anchor="w")
        self.measurements.pack(fill="x")

        NavigationToolbar2Tk(self.canvas, parent).pack(side="bottom", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.figure.text(0.5, 0.5, "Connect, then take a screenshot, go live, or acquire —\n"
                                   "or open a saved file.", color="#9aa3ab", ha="center")
        self.canvas.draw()
        self._measure_job = None

    # -- small widget helpers -----------------------------------------------

    def _button(self, parent, text, command, *, needs_scope=True) -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command)
        if needs_scope:
            self._buttons.append(button)
        return button

    def _check(self, parent, key, text, row, column) -> None:
        var = self._vars.setdefault(key, tk.BooleanVar())
        ttk.Checkbutton(parent, text=text, variable=var).grid(row=row, column=column, sticky="w",
                                                              padx=(0, 8))

    def _entry(self, parent, key, text, row) -> None:
        var = self._vars.setdefault(key, tk.StringVar())
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=var, width=12).grid(row=row, column=1, columnspan=2,
                                                           sticky="w", pady=1)

    def _combo(self, parent, key, text, choices, row) -> None:
        var = self._vars.setdefault(key, tk.StringVar())
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w")
        ttk.Combobox(parent, textvariable=var, values=list(choices), width=12,
                     state="readonly").grid(row=row, column=1, columnspan=2, sticky="w", pady=1)

    # -- the worker ---------------------------------------------------------
    #
    # One exchange with the scope at a time, on a background thread. A click
    # while one is running is queued; live frames run only when nothing else
    # is waiting, so a click is never starved by the live view.

    def _submit(self, label: str, work, done=None) -> None:
        """Run ``work()`` on the worker; call ``done(result)`` back on this thread."""
        if self._busy:
            if all(pending[0] != label for pending in self._pending):
                self._pending.append((label, work, done, False))
                if self._live is None:
                    self.status.set(f"{label} — queued")
            return
        self._start(label, work, done, live=False)

    def _start(self, label: str, work, done, *, live: bool) -> None:
        self._busy = True
        if not live:
            self.status.set(f"{label}…")
            self.root.config(cursor="watch")

        def target():
            try:
                self._results.put((done, work(), None, live))
            except Exception as exc:  # noqa: BLE001 - reported in the window
                self._results.put((None, None, exc, live))

        threading.Thread(target=target, daemon=True).start()

    def _start_next(self) -> None:
        if self._busy:
            return
        if self._pending:
            label, work, done, live = self._pending.pop(0)
            self._start(label, work, done, live=live)
        elif self._live is not None and self.scope is not None:
            self._live.points = int(self.points.get())
            self._start("live frame", self._live.read, self._show_live_frame, live=True)

    def _poll(self) -> None:
        # One finished job per tick, never a loop until the queue is empty: in
        # live mode each result starts the next frame, and a frame that comes
        # back faster than the last one is drawn would keep this method busy
        # forever and freeze the window.
        try:
            done, result, error, live = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            self._progress = None
            self.root.config(cursor="")
            if error is not None:
                if live:
                    self._stop_live()
                self._report(error)
                self._start_next()
            elif live:
                # Start the next transfer first, then draw this frame while it
                # runs --- drawing takes a noticeable fraction of a frame.
                self._start_next()
                done(result)
            else:
                if done is not None:
                    done(result)
                self._start_next()
        if self._busy and self._progress is not None:
            self.status.set(self._describe_progress())
        self.root.after(30, self._poll)

    def _report(self, error: Exception) -> None:
        self.status.set(f"Error: {error}")
        if not isinstance(error, ScopeError):
            messagebox.showerror("HP 54645D", f"{type(error).__name__}: {error}")

    def _enable(self) -> None:
        state = "normal" if self.scope is not None else "disabled"
        for button in self._buttons:
            button.configure(state=state)
        self.live_toggle.configure(state=state)
        self.connect_button.configure(state="disabled" if self._connecting else "normal")

    # -- connection ---------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.scope is not None:
            self._stop_live()
            scope = self.scope
            self._submit("Disconnecting", scope.close, lambda _: self._disconnected())
            return

        address, baud, flow = self.address.get(), int(self.baud.get()), self.flow.get()

        def work():
            scope = HP54645D(address, baud_rate=baud, flow_control=flow)
            try:
                return scope, scope.read_settings()
            except Exception:
                scope.close()
                raise

        def done(result):
            self._connecting = False
            self.scope, settings = result
            self._baud = baud
            self._set_connected(True)
            self._load(settings)
            self.status.set(f"Connected to {self.scope.identity}.")

        self._connecting = True
        self._enable()
        self._submit(f"Connecting to {address}", work, done)
        # If the connection fails, _poll reports it; release the button then.
        self.root.after(200, self._release_connect_button)

    def _release_connect_button(self) -> None:
        if self._busy:
            self.root.after(200, self._release_connect_button)
        else:
            self._connecting = False
            self._enable()

    def _disconnected(self) -> None:
        self.scope = None
        self._set_connected(False)
        self.status.set("Disconnected.")

    def _set_connected(self, connected: bool) -> None:
        self.connect_button.configure(text="Disconnect" if connected else "Connect")
        self.identity.set(self.scope.identity if connected and self.scope else "")
        self._enable()

    def _on_close(self) -> None:
        if self._progress is not None:
            source, done, total, _ = self._progress
            left = transfer_seconds(total - done, self._baud)
            if not messagebox.askyesno(
                "HP 54645D",
                f"A full-memory transfer is running ({source}, about "
                f"{format_duration(left)} left). The scope will keep sending until it is "
                f"done, and connecting again waits for that.\n\nClose anyway?",
            ):
                return
        self._live = None
        if self.scope is not None:
            self.scope.close()
        self.root.destroy()

    # -- settings -----------------------------------------------------------

    def _refresh(self) -> None:
        self._submit("Reading settings", self.scope.read_settings, self._loaded)

    def _loaded(self, settings: Settings) -> None:
        self._load(settings)
        self.status.set("Settings read from the scope.")

    def _load(self, s: Settings) -> None:
        """Fill the form from settings read off the scope."""
        self.settings = s
        if self._live is not None:
            self._live.use(s)
        v = self._vars
        for n, a in s.analog.items():
            v["analog", n, "displayed"].set(a.displayed)
            v["analog", n, "bandwidth_limit"].set(a.bandwidth_limit)
            v["analog", n, "invert"].set(a.invert)
            v["analog", n, "volts_per_div"].set(format_si(a.volts_per_div))
            v["analog", n, "offset_v"].set(format_si(a.offset_v))
            v["analog", n, "coupling"].set(a.coupling)
            v["analog", n, "probe"].set(a.probe)
        v["timebase", "mode"].set(s.timebase.mode)
        v["timebase", "seconds_per_div"].set(format_si(s.timebase.seconds_per_div))
        v["timebase", "delay_s"].set(format_si(s.timebase.delay_s))
        v["timebase", "reference"].set(s.timebase.reference)
        t = s.trigger
        for field in ("mode", "type", "source", "slope", "coupling", "reject"):
            v["trigger", field].set(getattr(t, field))
        v["trigger", "level_v"].set(format_si(t.level_v))
        v["trigger", "holdoff_s"].set(format_si(t.holdoff_s))
        v["trigger", "noise_reject"].set(t.noise_reject)
        for n in DIGITAL_CHANNELS:
            v["digital", n].set(n in s.digital.displayed)
        for pod, (kind, volts) in s.digital.thresholds.items():
            v["threshold", pod, "kind"].set(kind)
            v["threshold", pod, "volts"].set(format_si(volts))
        v["acquire", "type"].set(s.acquire.type)
        v["acquire", "count"].set(str(s.acquire.count))

    def _form(self) -> Settings:
        """The settings the form describes: the last ones read, with the edits.

        Raises:
            ValueError: If a number in the form is not a number.
        """
        s = copy.deepcopy(self.settings)
        v = self._vars
        for n, a in s.analog.items():
            a.displayed = v["analog", n, "displayed"].get()
            a.bandwidth_limit = v["analog", n, "bandwidth_limit"].get()
            a.invert = v["analog", n, "invert"].get()
            a.range_v = parse_si(v["analog", n, "volts_per_div"].get()) * VERTICAL_DIVISIONS
            a.offset_v = parse_si(v["analog", n, "offset_v"].get())
            a.coupling = v["analog", n, "coupling"].get()
            a.probe = v["analog", n, "probe"].get()
        s.timebase.mode = v["timebase", "mode"].get()
        s.timebase.range_s = (parse_si(v["timebase", "seconds_per_div"].get())
                              * HORIZONTAL_DIVISIONS)
        s.timebase.delay_s = parse_si(v["timebase", "delay_s"].get())
        s.timebase.reference = v["timebase", "reference"].get()
        t = s.trigger
        for field in ("mode", "type", "source", "slope", "coupling", "reject"):
            setattr(t, field, v["trigger", field].get())
        t.level_v = parse_si(v["trigger", "level_v"].get())
        t.holdoff_s = parse_si(v["trigger", "holdoff_s"].get())
        t.noise_reject = v["trigger", "noise_reject"].get()
        if self.scope is not None and self.scope.has_digital:
            s.digital.displayed = tuple(n for n in DIGITAL_CHANNELS if v["digital", n].get())
            s.digital.thresholds = {
                pod: (v["threshold", pod, "kind"].get(),
                      parse_si(v["threshold", pod, "volts"].get()))
                for pod in s.digital.thresholds
            }
        s.acquire.type = v["acquire", "type"].get()
        s.acquire.count = int(v["acquire", "count"].get() or s.acquire.count)
        return s

    def _apply(self) -> None:
        """Hand the form to ``scope.apply``, which sends only what differs."""
        try:
            wanted = self._form()
            count = changes(self.settings, wanted).count
        except ValueError as exc:
            self.status.set(f"Not applied: {exc}")
            return
        if not count:
            self.status.set("Nothing changed.")
            return
        current = self.settings

        def work():
            try:
                return self.scope.apply(wanted, current=current), []
            except ApplyError as exc:
                return exc.settings, exc.failures

        def done(result):
            settings, failures = result
            self._load(settings)
            if failures:
                self.status.set("Some changes were refused — " + " | ".join(failures))
            else:
                self.status.set(f"Applied {count} change(s); form re-read from the scope.")

        self._submit("Applying", work, done)

    # -- run control --------------------------------------------------------

    def _run_control(self, method: str) -> None:
        def work():
            getattr(self.scope, method)()
            return self.scope.read_settings() if method == "autoscale" else None

        def done(settings):
            if settings is not None:
                self._load(settings)
            self.status.set(f"{method.capitalize()} sent.")

        self._submit(method.capitalize(), work, done)

    # -- waveforms ----------------------------------------------------------

    def _screenshot(self) -> None:
        points = int(self.points.get())
        self._submit("Reading the screen",
                     lambda: self.scope.read_screen(points=points), self._show_capture)

    def _acquire(self) -> None:
        try:
            timeout = float(self.acquire_timeout.get())
        except ValueError:
            self.status.set("The acquire timeout must be a number of seconds.")
            return
        points = int(self.points.get())

        def work():
            settings = self.scope.read_settings()
            analog, digital = settings.displayed_analog, settings.digital.displayed
            if not analog and not digital:
                raise ScopeError("nothing is displayed; tick a channel's Display and Apply")
            return self.scope.acquire(analog, digital, timeout_s=timeout, points=points)

        self._submit("Waiting for a trigger", work, self._show_capture)

    def _show_capture(self, capture) -> None:
        self._load(capture.settings)
        self._draw(capture)
        names = [f"ch{n}" for n in capture.analog] + [f"D{n}" for n in capture.digital]
        note = {"acquisition": "  The scope is stopped on this capture; Run resumes.",
                "memory": "  The scope is stopped; Run resumes."}.get(capture.origin, "")
        self.status.set(f"{capture.origin.capitalize()}: {', '.join(names)}, "
                        f"{capture.points:,} points, at {capture.timestamp:%H:%M:%S}.{note}")

    def _draw(self, capture) -> None:
        self.capture = capture
        plot_capture(capture, self.figure, layout=self.layout.get())
        self.canvas.draw()
        if self.figure.axes:
            # Measure what is in view: again whenever the view changes.
            self.figure.axes[0].callbacks.connect(
                "xlim_changed", lambda _axes: self._schedule_measurements())
        self._update_measurements()

    def _schedule_measurements(self) -> None:
        """Re-measure shortly after the view stops changing, not on every step of a pan."""
        if self._measure_job is not None:
            self.root.after_cancel(self._measure_job)
        self._measure_job = self.root.after(150, self._update_measurements)

    def _update_measurements(self) -> None:
        self._measure_job = None
        capture = self.capture
        self.measurements.delete(*self.measurements.get_children())
        if capture is None or not self.figure.axes:
            return
        scale, unit = time_axis(capture)
        low, high = (x * scale for x in self.figure.axes[0].get_xlim())
        self.window_label.set(f"Over the visible time range, {format_si(low)}s to "
                              f"{format_si(high)}s — zoom or pan to change it.")
        for trace, found in measure(capture, start=low, stop=high).items():
            values = {m.name: format_measurement(m) for m in found}
            self.measurements.insert("", "end", values=[trace] + [
                values.get(column, "") for column in _MEASURE_COLUMNS[1:]])

    def _redraw(self) -> None:
        """Switch layout without asking the scope for anything."""
        if self.capture is not None:
            self._draw(self.capture)

    # -- live view ----------------------------------------------------------

    def _toggle_live(self) -> None:
        if not self.live_var.get():
            frames = self._live.frames if self._live else 0
            self._stop_live()
            self.status.set(f"Live view stopped after {frames} frames.")
            return
        if self.scope is None:
            self.live_var.set(False)
            return
        self._live = self.scope.live(points=int(self.points.get()))
        if self.settings is not None:
            self._live.use(self.settings)
        self.status.set("Live view starting…")
        self._start_next()

    def _stop_live(self) -> None:
        self._live = None
        self.live_var.set(False)

    def _show_live_frame(self, capture) -> None:
        """Draw a frame. The form is not refilled: that would undo typing in it."""
        live = self._live
        if live is None:
            return
        self._draw(capture)
        self.status.set(f"Live: {live.frames} frames, {live.seconds_per_frame:.2f} s per "
                        f"frame at {live.points} points. Untick Live to stop.")

    # -- full memory --------------------------------------------------------

    def _full_memory(self) -> None:
        """Stop, measure the records, ask which to read, then read and save them."""
        self._stop_live()

        def size():
            self.scope.stop()
            settings = self.scope.read_settings()
            return settings, self.scope.memory_sizes(settings=settings)

        def ask(result):
            settings, sizes = result
            self._load(settings)
            chosen = MemoryDialog(self.root, sizes, self._baud).chosen
            if not chosen:
                self.status.set("Full-memory readout cancelled. The scope is stopped.")
                return
            path = filedialog.asksaveasfilename(
                defaultextension=".npz", filetypes=_DATA_FILES,
                initialfile=f"hp54645d_memory_{time.strftime('%Y%m%d_%H%M%S')}.npz")
            if not path:
                self.status.set("Full-memory readout cancelled. The scope is stopped.")
                return
            analog = [int(s[-1]) for s in chosen if s.startswith("ANALOG")]
            digital = [n for n in settings.digital.displayed
                       if f"POD{pod_of(n)}" in chosen]
            self._submit("Reading the full memory",
                         lambda: self._read_memory(analog, digital, path), self._memory_saved)

        self._submit("Measuring the acquisition memory", size, ask)

    def _read_memory(self, analog, digital, path):
        """On the worker: read, then save before anything else can go wrong."""
        started = time.monotonic()
        current = {"source": None, "since": started}

        def progress(source, done, total):
            if source != current["source"]:  # time each source from its own start
                current.update(source=source, since=time.monotonic())
            self._progress = (source, done, total, current["since"])

        capture = self.scope.read_memory(analog, digital, progress=progress)
        return capture, capture.save(path), time.monotonic() - started

    def _memory_saved(self, result) -> None:
        capture, written, seconds = result
        self._show_capture(capture)
        self.status.set(f"Full memory: {capture.points:,} points in "
                        f"{format_duration(seconds)}, saved to "
                        f"{', '.join(str(p) for p in written)}. The scope is stopped.")

    def _describe_progress(self) -> str:
        source, done, total, started = self._progress
        elapsed = time.monotonic() - started
        rate = done / elapsed if elapsed > 0 and done else self._baud / 10
        left = (total - done) / rate
        return (f"Reading {source}: {done / total:.0%} of {total:,} points, about "
                f"{format_duration(left)} left for this source. Cannot be interrupted.")

    # -- files --------------------------------------------------------------

    def _open_file(self) -> None:
        """Load a saved capture and show it. Needs no scope, and does not touch one.

        The form is left alone: it describes the scope, and filling it from
        a file would make the next *Apply* send the file's settings to the
        instrument.
        """
        path = filedialog.askopenfilename(
            title="Open a saved capture",
            filetypes=[("Captures", "*.npz *.csv"), ("NumPy archive", "*.npz"),
                       ("CSV", "*.csv")])
        if not path:
            return
        self._stop_live()
        self.status.set(f"Loading {path}…")
        self.root.config(cursor="watch")
        outcome: dict = {}

        def load():
            try:
                outcome["capture"] = load_capture(path)
            except Exception as exc:  # noqa: BLE001 - reported in the window
                outcome["error"] = exc

        # Its own thread, not the scope's worker: a file needs no instrument,
        # and should not wait behind a full-memory transfer.
        loader = threading.Thread(target=load, daemon=True)
        loader.start()

        def finished():
            if loader.is_alive():
                self.root.after(50, finished)
                return
            self.root.config(cursor="")
            if "error" in outcome:
                self._report(outcome["error"])
                return
            capture = outcome["capture"]
            self._draw(capture)
            names = [f"ch{n}" for n in capture.analog] + [f"D{n}" for n in capture.digital]
            self.status.set(f"Opened {capture.file.name}: {', '.join(names)}, "
                            f"{capture.points:,} points, {capture.origin} from "
                            f"{capture.timestamp:%Y-%m-%d %H:%M:%S}. Zoom to measure a stretch.")

        finished()

    def _save_data(self) -> None:
        if self.capture is None:
            self.status.set("Nothing to save yet.")
            return
        capture = self.capture
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv"), ("NumPy archive", "*.npz")],
            initialfile=f"hp54645d_{capture.timestamp:%Y%m%d_%H%M%S}.csv")
        if path:
            written = capture.save(path)
            self.status.set("Saved " + ", ".join(str(p) for p in written))

    def _save_png(self) -> None:
        if self.capture is None:
            self.status.set("Nothing to save yet.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG", "*.png")],
            initialfile=f"hp54645d_{self.capture.timestamp:%Y%m%d_%H%M%S}.png")
        if path:
            self.figure.savefig(path, dpi=120, facecolor=SCREEN)
            self.status.set(f"Saved {path}")

    def _send_command(self) -> None:
        command = self.command.get().strip()
        if not command:
            return
        if command.endswith("?"):
            self._submit(command, lambda: self.scope.query(command),
                         lambda answer: self.status.set(f"{command}  →  {answer}"))
        else:
            self._submit(command, lambda: self.scope.write(command),
                         lambda _: self.status.set(f"{command}  →  accepted"))


class MemoryDialog:
    """Which records to read, with how long each takes. Modal.

    After it closes, :attr:`chosen` holds the source names picked, or is
    empty if cancelled.
    """

    def __init__(self, parent: tk.Tk, sizes: dict[str, int], baud_rate: int) -> None:
        """Show the dialog and wait for it to close."""
        self.chosen: list[str] = []
        self._sizes, self._baud = sizes, baud_rate
        top = self._top = tk.Toplevel(parent)
        top.title("Full-memory readout")
        top.transient(parent)
        top.resizable(False, False)
        frame = ttk.Frame(top, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="The scope is stopped. Its memory holds:",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self._picked: dict[str, tk.BooleanVar] = {}
        for source, points in sizes.items():
            var = tk.BooleanVar(value=source.startswith("ANALOG"))
            self._picked[source] = var
            name = source.replace("ANALOG", "Channel ").replace(
                "POD1", "Pod 1 (D0–D7)").replace("POD2", "Pod 2 (D8–D15)")
            ttk.Checkbutton(
                frame, variable=var, command=self._update,
                text=f"{name}: {points:,} points, about "
                     f"{format_duration(transfer_seconds(points, baud_rate))}",
            ).pack(anchor="w", pady=1)
        self._total = tk.StringVar()
        ttk.Label(frame, textvariable=self._total).pack(anchor="w", pady=(8, 0))
        ttk.Label(frame, foreground="#8a4b00", wraplength=420, justify="left",
                  text="Once a transfer has started it cannot be interrupted: the scope sends "
                       "every byte. The window stays usable, but other actions wait. You "
                       "choose the file next; it is saved as soon as the data is in.").pack(
            anchor="w", pady=(6, 10))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        self._ok = ttk.Button(buttons, text="Read and save…", command=self._accept)
        self._ok.pack(side="right")
        ttk.Button(buttons, text="Cancel", command=top.destroy).pack(side="right", padx=6)
        self._update()

        top.grab_set()
        top.wait_window()

    def _update(self) -> None:
        picked = [s for s, var in self._picked.items() if var.get()]
        seconds = sum(transfer_seconds(self._sizes[s], self._baud) for s in picked)
        self._total.set(f"Selected: about {format_duration(seconds)} in total." if picked
                        else "Nothing selected.")
        self._ok.configure(state="normal" if picked else "disabled")

    def _accept(self) -> None:
        self.chosen = [s for s, var in self._picked.items() if var.get()]
        self._top.destroy()


def main() -> None:
    """Start the GUI."""
    try:  # crisp text on high-DPI Windows displays
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - not Windows, or too old; purely cosmetic
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
