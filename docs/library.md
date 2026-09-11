# Using the library

```python
from hp54645d import HP54645D
```

One class, `HP54645D`, plus the data it returns. Everything raises
`ScopeError` (or a subclass) on failure — never returns a status.

## Connecting

```python
scope = HP54645D("ASRL5::INSTR", baud_rate=19200, flow_control="xon_xoff")
...
scope.close()

# or, closing automatically:
with HP54645D("ASRL5::INSTR", baud_rate=19200) as scope:
    ...
```

| Argument | Meaning |
|---|---|
| `address` | VISA resource. `ASRL5::INSTR` is COM5; `GPIB0::7::INSTR` would be HP-IB |
| `baud_rate` | 1200, 2400, 9600 or 19200 — must match the scope's front panel |
| `flow_control` | `"xon_xoff"` (the scope's *XON*), `"dtr_dsr"`, or `"none"` |
| `timeout_s` | Default I/O timeout, 10 s |

The scope cannot report its line settings. A mismatch gives **silence**, which
surfaces as `ScopeTimeout` with a hint about what to check.

`scope.identity` is the `*IDN?` string; `scope.model` is `"54645D"`;
`scope.has_digital` is true for the D model.

## Three ways to get waveforms

| | What | Changes the scope? | Time at 19200 baud |
|---|---|---|---|
| `read_screen()` | what the screen shows now | no | ~1.2 s per channel (2000 points) |
| `acquire()` | a fresh single capture | shows requested channels, leaves it stopped | trigger + transfer |
| `read_memory()` | every stored point | stops it | ~4–9 min per channel |

### `read_screen()` — what the screen shows, changing nothing

```python
capture = scope.read_screen()                  # every displayed channel
capture = scope.read_screen(analog=[1])        # just channel 1
capture = scope.read_screen(digital=[0, 1, 2])
```

Sends no command that changes anything on the scope: no re-arm, no run/stop,
no setting. Only queries, plus the waveform transfer's own source/format
selection, which is not visible on the instrument.

- Only **displayed** channels can be read — the scope has nothing to send for
  a hidden one. Asking for one raises `ScopeError` straight away.
- If the scope is **running**, each channel is its latest acquisition at the
  moment it is read, and each transfer takes about a second. Two channels can
  then come from different triggers. **Press Stop first** for a picture where
  everything comes from one trigger.
- The timebase must be in `MAIN` mode (not ROLL, XY or DELAYED).

`points` sets the analog record length: 100, 200, 250, 400, 500, 800, 1000,
2000 or 4000, always spanning the whole screen. Transfer time is nearly all
line time:

| points | time per channel |
|---|---|
| 2000 (the default record) | 1.20 s |
| 1000 | 0.70 s |
| 500 | 0.45 s |
| 250 | 0.32 s |

Digital pods always come at their own length (500 points).

### `live()` — screenshots, one after another

```python
for capture in scope.live(points=500):      # about 0.55 s per channel
    plot_capture(capture, fig)
    if done:
        break
```

Reads the settings once and reuses them between frames — reading them costs
about a second, more than a 500-point frame. It reads them again every
`refresh_s` (10 s), whenever a frame fails (a channel hidden on the front
panel makes its transfer time out; the frame is retried once), and whenever
you hand it fresh ones: `reader.use(settings)`, e.g. after an `apply()`.

`reader.read()` gives one frame, for your own loop. `reader.frames` and
`reader.seconds_per_frame` say how it is going.

Stale settings could only mislabel the graticule — each frame carries its own
scaling — so the refresh can be infrequent.

### `acquire()` — a new triggered capture

```python
capture = scope.acquire(analog=[1, 2], digital=[0, 1], timeout_s=5)
```

Switches on any requested channel that is hidden, arms a **single**
acquisition, waits for the trigger and reads every requested channel — all
from the same trigger. The scope is left **stopped** on that capture; call
`scope.run()` to carry on.

If nothing triggers within `timeout_s`, raises `TriggerTimeout` — after
stopping the pending acquisition, so the scope is immediately usable again.

### `read_memory()` — every stored point

```python
scope.single()                                  # optional: the deepest record
print(scope.memory_sizes())                     # {'ANALOG1': 1000000, 'POD1': 2000000}
capture = scope.read_memory(analog=[1], progress=print)
capture.save("captures/full.npz")
```

For a **stopped** scope: it sends `:STOP` first, so the memory cannot change
during a transfer that takes minutes. Stopping an already-stopped scope changes
nothing — on the bench the record was identical before and after.

The memory holds more than the screen, at the full sample rate. How much
depends on how the scope stopped — measured at 200 µs/div:

| | analog | digital pod |
|---|---|---|
| running (not readable: `read_memory` stops first) | 400,000 points, 2 ms | — |
| after **Stop** | 500,000 points, 2.5 ms | not measured |
| after **Single** | 1,000,000 points, 5 ms at 5 ns | 2,000,000 points at 2.5 ns |

`memory_sizes()` asks the scope (queries only) so you know before starting.
At one byte per point, 19200 baud moves about 1920 points a second: half a
million take about 4.5 minutes, a million about 9, a digital pod after Single
about 17.

- **It cannot be interrupted.** Once a source's transfer starts, the scope
  sends every byte whatever else it is told. Closing the program meanwhile
  leaves it sending into the void; the next connection waits for silence
  before its first question, which can take as long as the rest of the
  transfer.
- `progress(source, bytes_read, bytes_total)` is called as each chunk
  arrives.
- A digital pod is read whole — eight channels at a time.

## What you get back: `Capture`

```python
capture.analog[1].time_s        # numpy array, seconds relative to the trigger
capture.analog[1].volts         # numpy array, volts
capture.digital[0].levels       # numpy array of bool
capture.settings                # the scope's settings when it was read
capture.origin                  # "screen", "acquisition" or "memory"
capture.points                  # the longest record in it
capture.timestamp               # datetime

capture.save("captures/run1.csv")   # CSV: analog here, digital in run1_digital.csv
capture.save("captures/run1.npz")   # NumPy archive: smaller, faster, lossless
```

Analog and digital records have **different time axes**: a 2 ms screen gives
2000 analog points but 500 digital ones. Each trace carries its own `time_s`.

For a full-memory record, prefer `.npz`: a million points make about 25 MB of
CSV. The archive holds `ch1_time_s`, `ch1_v`, … per analog channel,
`pod1_time_s` and `d0`, … per logic channel, and `meta` — a JSON string with
the settings, origin and timestamp:

```python
import json, numpy as np
with np.load("captures/full.npz") as f:
    t, v = f["ch1_time_s"], f["ch1_v"]
    settings = json.loads(str(f["meta"]))["settings"]
```

### Loading a saved capture

```python
from hp54645d import load_capture

capture = load_capture("captures/full.npz")      # or run.csv, run_digital.csv, run_ch1.csv
capture.file                                     # where it came from
```

Gives back the same `Capture` a readout does, so plotting, measuring and
saving work on it unchanged — no scope needed.

- `.npz` comes back exactly, settings included.
- For **CSV**, open any of the files one save wrote; the ones beside it are
  loaded too. The settings come from the header's `# settings: {…}` JSON line.
  CSV files saved before that line existed (before 2026-09-11 14:00) still
  load: their per-channel scale is recovered from the header's words, and the
  timebase is then taken to be the whole record.
- A file this package did not write raises `ScopeError`.

## Measuring

```python
from hp54645d import measure

measure(capture)                          # every channel, the whole record
measure(capture, start=-2e-6, stop=2e-6)  # seconds from the trigger
# {'ch1': [Measurement(name='min', value=-0.0625, unit='V'), ...], 'D0': [...]}
```

| Analog | Logic |
|---|---|
| `min`, `max`, `pk-pk`, `mean`, `rms` (V) | `high` (% of the time) |
| `frequency` (Hz), `period` (s), `duty` (%) | `frequency`, `period`, `duty` |

`measure_analog(trace, start=…, stop=…)` and `measure_digital(...)` in
`hp54645d.analysis` do one trace.

Frequency, period and duty come from crossings of the middle of the swing,
with hysteresis at 40 % and 60 % of it, so noise on an edge is not counted as
extra edges. They need at least two rising edges in the window, and with
three or more, **regular** ones — gaps varying by less than 25 %. Otherwise
they are left out rather than guessed: on a flat stretch the "swing" is only
noise, and it crosses any threshold at random. Vectorised; a million points
take milliseconds.

## Plotting

```python
from hp54645d import plot_capture
import matplotlib.pyplot as plt

plot_capture(capture)           # a new figure, drawn like the scope screen
plt.show()

plot_capture(capture, my_fig)   # or into a figure you already have
plot_capture(capture, layout="overlay")   # all analog channels on one screen
```

- `layout="split"` (default) — one panel per analog channel, in volts over
  that channel's screen span with the 8 × 10 graticule.
- `layout="overlay"` — one screen, as on the scope: each channel placed by its
  own V/div and offset. When the channels' scales differ, the axis is in
  **divisions**, the legend gives each channel's scale, and a numbered marker
  on the left edge shows where each channel's 0 V is. When they share a scale,
  the axis stays in volts.

Logic channels get their own panel below in both. Dashed lines mark the
trigger time and level; the time ticks fall on the scope's divisions, counted
from the trigger.

Long records are drawn as a min/max envelope, so a one-sample spike still
shows at full height, and re-thinned whenever the visible time range changes —
zoom in with the matplotlib toolbar and the real samples appear. Logic traces
are drawn from their transitions, exact at any length.

## Reading settings

```python
s = scope.read_settings()       # queries only, about a second
s.analog[1].range_v             # 16.0   full screen, 8 divisions
s.analog[1].volts_per_div       # 2.0
s.analog[1].displayed           # True
s.timebase.range_s              # 0.002  full screen, 10 divisions
s.timebase.seconds_per_div      # 0.0002
s.trigger.source                # "ANALOG1"
s.digital.displayed             # (0, 1)
s.digital.thresholds            # {1: ("TTL", 1.4), 2: ("TTL", 1.4)}
```

Spans are **full screen**, as the instrument has them. `volts_per_div` and
`seconds_per_div` are there for when you think in divisions.

## Changing settings

The simplest way is to edit what you read and hand it back:

```python
s = scope.read_settings()
s.analog[1].range_v = 8.0
s.trigger.level_v = 1.0
s = scope.apply(s)             # sends only those two, then reads everything back
```

`apply()` works out the difference with `hp54645d.changes(current, wanted)` —
a pure function you can call yourself to see what would be sent — and sends it
section by section, channels before the trigger (the trigger level's limits
depend on the channel's range). It returns the settings **read back
afterwards**, which can differ from what you asked for where the scope rounded
or clamped. If a part is refused, the rest is still sent and `ApplyError` is
raised, carrying `.failures` and the read-back `.settings`. Numbers count as
equal within 1 part in 10⁴, so a value the scope rounded is not re-sent.

Or change individual things directly. Each `configure_*` changes only what you
pass; everything else is left alone.

```python
scope.configure_analog(1, displayed=True, range_v=16, offset_v=2.5,
                       coupling="DC", probe="X10", bandwidth_limit=False, invert=False)
scope.configure_timebase(mode="MAIN", range_s=2e-3, delay_s=0, reference="CENTER")
scope.configure_trigger(mode="NORMAL", type="EDGE", source="ANALOG1",
                        level_v=2.5, slope="POSITIVE", coupling="DC",
                        reject="OFF", holdoff_s=200e-9, noise_reject=False)
scope.configure_digital(displayed={0: True, 1: True},
                        thresholds={1: "TTL", 2: ("USERDEF", 2.0)})
scope.configure_acquire(type="AVERAGE", count=16)
```

| Setting | Values |
|---|---|
| `coupling` | `AC`, `DC`, `GND` |
| `probe` | `X1`, `X10`, `X20`, `X100` |
| timebase `mode` | `MAIN`, `DELAYED`, `XY`, `ROLL` |
| `reference` | `LEFT`, `CENTER`, `RIGHT` |
| trigger `mode` | `AUTLEVEL`, `AUTO`, `NORMAL` |
| trigger `type` | `EDGE`, `TV`, `GLITCH`, `ADVANCED`, `PATTERN` |
| trigger `source` | `ANALOG1`, `ANALOG2`, `DIGITAL0` … `DIGITAL15`, `LINE` |
| `slope` | `POSITIVE`, `NEGATIVE` |
| threshold | `TTL`, `CMOS`, `ECL`, or `("USERDEF", volts)` |
| acquire `type` | `NORMAL`, `AVERAGE`, `PEAK`, `REALTIME`; `count` 4 … 256 |

Choices are case-insensitive and also accept the instrument's short forms
(`"cent"`, `"anal1"`). A choice that does not exist raises `ValueError` before
anything is sent.

After sending, each `configure_*` reads the scope's error queue and raises
`CommandError` — carrying the scope's own codes and messages — if it
complained.

> **The scope clamps rather than complains.** A value outside what the
> instrument can do is usually clamped to the nearest limit without any
> error: asking for a 1 MV range gave 400 V. If the exact value matters, read
> it back with `read_settings()`. The GUI does this after every *Apply*.

## Run control and raw commands

```python
scope.run(); scope.stop(); scope.single(); scope.autoscale()

scope.query(":MEASure:FREQuency? ANALog1")    # anything the API does not cover
scope.write(":DISPlay:GRID FULL")              # raises CommandError if refused
scope.errors()                                 # read and empty the error queue
```

The full command set is in chapter 8 of the programmer's guide in
[manuals/](manuals/).

## Errors

| Exception | When |
|---|---|
| `ScopeError` | Base class for everything below, and misc failures |
| `ScopeTimeout` | No answer: wrong address/baud, printer mode, or a hidden channel queried by hand |
| `CommandError` | The scope's error queue was not empty after a command; `.errors` has `(code, message)` pairs |
| `ApplyError` | `apply()` had parts refused; `.failures` and the read-back `.settings` |
| `TriggerTimeout` | `acquire()` saw no trigger in time; the acquisition was stopped |

## Threads

Each exchange with the scope holds a lock, so a GUI thread and a worker thread
cannot interleave a query with someone else's answer. One `HP54645D` per
port — the serial line is not shareable between processes; a second program
gets `ScopeError` saying the port is in use.
