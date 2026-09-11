# How it is built

## Layers

```
gui.py          the window: form, buttons, dialogs, a worker thread — no instrument logic
  │
  ├── plot.py   Capture → matplotlib figure; envelope thinning for long records
  │
  ├── analysis.py  measurements over a time window                   (no I/O)
  │
scope.py        HP54645D: settings, apply(), run control,
  │             read_screen(), acquire(), read_memory()
  ├── live.py       LiveReader: repeated readouts, settings reuse and recovery
  ├── settings.py   dataclasses, the instrument's vocabulary, changes()   (no I/O)
  ├── waveform.py   preamble maths, traces, Capture, CSV / npz save + load
  │                 (files only, never the instrument)
  │
transport.py    the line: write, query, chunked binary blocks, error queue, drain
                — the only pyvisa user
```

Each layer only calls the one below it. The library is `transport` up to
`plot`; the GUI is a client of the library with no private access.

**The GUI holds no instrument logic.** What to send when settings change
(`changes()` / `apply()`), how a live view keeps up (`LiveReader`), how the
full memory is sized and read (`memory_sizes()` / `read_memory()`) — all of
that is library, so a script can do exactly what the window does. The GUI owns
the form, the buttons, the worker thread and the dialogs, and nothing it does
could not be written as a ten-line script.

`settings.py` and `waveform.py` are pure data and arithmetic. That is where
the mistakes that produce *plausible wrong numbers* would live — preamble
scaling, bit order, units, deciding a value has not changed — so they are kept
free of I/O and tested directly.

## The line (`transport.py`)

- **pyvisa with the pyvisa-py back-end.** No NI-VISA needed; RS-232 goes
  through pyserial. HP-IB would work through NI-VISA with the same code.
- **Line settings** are applied on open: 8 data bits, no parity, 1 stop bit,
  the given baud rate and handshake (§4-8). The scope cannot negotiate them.
- **Binary blocks are read by length**, never up to the terminator. The data
  is binary and a sample of value 10 *is* the newline; reading to it would
  cut the record short silently.
- **Blocks are read in chunks**, each small enough to arrive well inside the
  timeout, so a record of any size works and progress can be reported.
- **Every exchange holds a lock**, so the GUI's threads cannot interleave.
- **After a timeout, and on connecting, the input is drained until the line
  is quiet.** A late answer left in the buffer would otherwise be taken as the
  reply to the next query, and every answer after it would be off by one.
  Waiting for *silence* rather than a newline matters: the scope finishes a
  binary block whatever it is told, and a binary stream need not contain a
  newline at all. (The first version waited for a newline, gave up after
  300 ms in the middle of a stream, and the next query landed in the data.)
- **The error queue** (`:SYSTem:ERRor?`) is read after every settings change.

## Settings (`settings.py`)

Choices are named by the long form of the instrument's own mnemonic,
upper-cased — `CENTER` for `CENTer`, `AUTLEVEL` for `AUTLevel` — so every
value traces straight to the manual. The scope *answers* in short form
(`CENT`, `ANAL1`, `NORM,EDGE`); `Choices` translates both ways, so nothing
above that module ever sees a short form.

Spans are full screen, as the instrument has them (`:ANALog1:RANGe 16` is
16 V over 8 divisions). The GUI shows per-division values and converts.

## The screenshot (`read_screen`)

The scope keeps the displayed record in acquisition memory, and
`:WAVeform:DATA?` returns it **without re-arming or stopping anything** —
checked on the bench, with the scope running. With `:WAVeform:POINts NORMal`
the record is the screen: 2000 analog points across the 10 divisions.

So a screenshot is: read the settings (to know what is displayed and how it is
scaled), then transfer each displayed source. It never sends a command that
changes state; the tests assert exactly that.

Two constraints come from the instrument:

- **Only displayed sources can be read.** For a hidden one the scope does not
  answer at all — the query times out and `-221 Settings conflict` lands in
  the error queue. So `read_screen` checks `:STATus?` first and refuses up
  front instead of waiting for a timeout.
- **Digital channels come a pod at a time.** `POD1` is D0–D7, `POD2` is
  D8–D15, one byte per sample, and a pod can only be read if at least one of
  its channels is displayed. A pod record is shorter than an analog one: 500
  points against 2000 over the same 2 ms.

## Changing settings (`apply`)

`changes(current, wanted)` in `settings.py` is a pure function: two snapshots
in, `configure_*` arguments out. Choices are compared in canonical form, and
numbers within 1 part in 10⁴ — the scope answers in six significant figures,
and a value it rounded must not be re-sent. `apply()` sends the result
section by section (channels before the trigger, whose level limits depend on
their range), carries on past a refused part, and reads everything back.

## The live view (`live.py`)

A live frame is a screenshot with the one-second settings read taken out:
`read_screen(points=…, settings=cached)`. What is left is almost entirely
line time — 500 bytes at 19200 baud is a quarter of a second — plus a fixed
~0.2 s for the preamble and the handshakes. Measured on the bench, two
channels at 500 points ran at 1.1 s per frame.

`LiveReader` re-reads the settings every ten seconds, whenever it is handed
fresh ones (the GUI does after every *Apply*), and whenever a frame fails — a
channel hidden on the front panel makes its transfer time out and leaves
`-221` in the error queue, which the reader clears before retrying the frame
once with fresh settings. Stale settings could only mislabel the graticule —
the volts and times arrive with each frame's own preamble — so the refresh can
be infrequent.

The GUI's worker runs one exchange at a time. Clicks during a frame are
queued and run before the next frame, and the next transfer starts before the
current frame is drawn, so drawing costs no line time.

## The full memory (`read_memory`)

`:WAVeform:POINts ALL` makes the next transfer the whole acquisition memory,
not the screen. It is read only from a stopped scope: `read_memory` sends
`:STOP` first, which on an already-stopped scope changes nothing (the bench
record was identical before and after), and guarantees the memory cannot
change during a transfer that takes minutes.

How much there is depends on how the scope stopped, and there is no way to
choose a smaller part — `ALL` accepts only the scope's own length. So
`memory_sizes()` asks first (queries only) and the GUI shows the estimate
before committing.

Once the scope starts sending a block it sends all of it: a query sent in the
middle of a half-million-point transfer was ignored and the remaining 470,824
bytes still arrived, over four minutes. There is no remote abort. Hence the
confirmation dialog, and hence the quiet-line drain on connecting.

Plotting a million points directly would be slow and pointless at screen
resolution, so `plot.py` draws a min/max envelope of 3000 bins — spikes keep
their full height — and recomputes it on every change of the visible range.
Zoomed in far enough, the plot shows the raw samples.

## Acquiring (`acquire`)

The manual recommends `:DIGitize` for a fresh capture. **This package does not
use it**, because of what it does when nothing triggers:

`:DIGitize` blocks the scope's command parser until the acquisition
completes. With no trigger, every later command — including `:STOP` — is
queued behind it, and a serial break does not help. On the bench, the only way
out was a front-panel key (*Autoscale* worked; turning the trigger level knob
did not). A GUI with an *Acquire* button cannot risk that.

Instead:

1. `:STOP`, then read `:TER?` once to clear the trigger-event register.
2. `:SINGle` — arms one acquisition but leaves the parser free (checked: the
   scope still answers `*IDN?` while armed).
3. Poll `:TER?` until it reads 1.
4. Wait out the rest of the record (the trigger-to-right-edge time).
5. Transfer each source.

On a timeout, `:STOP` actually stops the armed single, and the scope is usable
immediately. All channels come from the same trigger, and the scope is left
showing the capture.

## Files and measurements

A saved capture loads back into the same `Capture` a readout produces, so
nothing downstream — plot, measurements, saving — knows or cares where it came
from. `.npz` stores the settings as JSON in a `meta` entry; CSV carries the
same JSON as one header comment, alongside human-readable lines. Settings are
rebuilt with `Settings.from_dict`, which ignores fields it does not know, so a
file from a later version still opens.

In the GUI, opening a file leaves the settings form alone: the form describes
the instrument, and filling it from a file would make the next *Apply* send
the file's settings to the scope. Files load on their own thread, not the
scope's worker, so opening one never waits behind a full-memory transfer.

`analysis.py` measures over a time window; the GUI passes the visible range of
the plot, re-measuring 150 ms after the view stops changing. Frequency is
only reported for regular edges: the first version happily turned the noise
on a flat stretch into a "frequency" — its swing was the noise itself, so the
thresholds sat inside it — and a test on a synthetic signal caught it before
it reached the window. On the bench's million-point record it now gives
1.2327 kHz and 50.0 % for the whole record, and nothing for a flat top.

## What the bench taught us

Everything below was found on a 54645D, firmware A.02.08, over an FTDI
USB-serial adapter at 19200 baud with XON/XOFF, on 2026-09-11.

| Finding | Consequence |
|---|---|
| `:DIGitize` with no trigger locks the parser; only a front-panel key frees it | `acquire` uses `:SINGle` + `:TER?` polling instead |
| `:SINGle` leaves the parser free, and `:STOP` aborts it | a trigger timeout is recoverable |
| Waveform queries for a hidden source get no answer, only `-221` | check `:STATus?` before reading |
| `:WAVeform:DATA?` works while running, with no re-arm | the screenshot needs no state change |
| Pod records have 500 points where analog has 2000 | each trace keeps its own time axis |
| Out-of-range values are clamped silently (1 MV range → 400 V) | read back after changing; the GUI always does |
| Timebase mode is answered as `MAIN`; §2-6 of the guide says `NORMal` | the guide is inconsistent; `MAIN` is used |
| No query reports run/stop state; the wait-trigger status bit stays 0 while running | the GUI does not show run state |
| The screen record can be thinned: `POINts NORMal,500` → 500 points over the same screen in 0.45 s | live view at ~2 frames/s per channel |
| The memory holds 400 k points running, 500 k after Stop, 1 M after Single (analog, 200 µs/div); a pod 2 M | `read_memory` sizes first; estimates shown before reading |
| `POINts ALL,<n>` accepts only the scope's own length (`-222` otherwise) | no partial memory readout |
| A binary block is always sent to the end; a query sent meanwhile is ignored | transfers cannot be cancelled; drain waits for a quiet line |

## Known gaps

- **Digital bit order is not yet confirmed** against a known signal. Bit *n*
  of a pod byte is taken as channel `8 × (pod − 1) + n`. To check: wire D0 to
  the probe-adjust terminal, display D0 only, and take a screenshot — D0
  should show the 1.2 kHz square.
- **XON/XOFF and binary data.** With XON/XOFF, a byte of value 17 or 19 can
  be swallowed by the serial driver as a flow-control character. The
  length-exact read turns that into a timeout rather than a short record, but
  the transfer still fails. For an analog trace it takes a signal in the
  bottom division of the screen; not seen yet. **For digital pods it is
  likely**: a pod byte is eight logic levels, and 17 is simply D0 and D4 high
  together. Only all-low pods have been read so far. For real logic signals,
  switch the scope and the tool to DTR handshake with a full cable.
- **Averaging with `acquire`.** In `AVERAGE` mode, the trigger arrives before
  the average is complete; `acquire` waits one record's length, not *count*
  of them. Use `read_screen` after the average has settled instead.
- **Rare command corruption was seen twice**, both right after the scope had
  been stuck in `:DIGitize` and hit with a serial break. It could not be
  reproduced in over 250 later exchanges of every kind. The error-queue check
  and the GUI's read-back would show it if it recurred.
- **Only the edge trigger's parameters** are modelled. Glitch, pattern, TV and
  advanced triggers are reachable through `scope.write(...)`.

## Tests

`tests/fakes.py` simulates the serial line with answers **recorded from the
real scope** — including its behaviours: the silence for hidden sources, the
newline after blocks, the error queue. The tests pin:

- the preamble maths against real scaling, and the digital bit split;
- that `read_screen` sends nothing but `:WAVeform:` transfer selection;
- that `acquire` never sends `:DIGitize`, and stops on a trigger timeout;
- command order (probe before range before offset) and error reporting;
- the length-exact block read, with a newline byte inside the data, in
  chunks with progress; and a drain that waits for silence;
- `changes()`: rounding is not a change, choices compare canonically, a
  threshold voltage counts only for a user threshold;
- `apply()`: only differences sent, channels before the trigger, the rest
  still sent after a refusal, nothing at all when nothing differs;
- the live reader: settings read once, refreshed when old or handed over,
  recovery when the display changes underneath it;
- `read_memory()` stops first and reads `ALL`; `.npz` and CSV round trips;
- plotting a million points: a one-sample spike survives the envelope, and
  zooming in shows the real samples;
- loading: `.npz` exact; CSV from any of its files, and CSV from before the
  settings line; a foreign file refused;
- measurements on a signal built to known answers (1 kHz, 25 % duty, 0–5 V),
  with heavy noise, in a window with no edges, and at a million points;
- the GUI's number parsing, and the transfer-time estimate.

```bash
pytest -q        # no instrument needed
ruff check .
```

## Sources

Everything here was written from the HP 54645A/D Programmer's Guide and then
checked against a real instrument; the table above is where the two
disagreed. Command references (§) are to that guide — chapter 4 for RS-232,
chapter 8 for the full command list. It is HP's copyright and not included;
[manuals/README.md](manuals/README.md) says where to find it.
