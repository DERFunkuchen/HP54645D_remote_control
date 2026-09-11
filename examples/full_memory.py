"""Read every point the HP 54645D has stored for channel 1, save it, plot it.

The scope is stopped first (read_memory does that) and left stopped. For the
deepest record --- a million points at 200 us/div --- press Single before
running this. At 19200 baud a million points take about nine minutes, and the
transfer cannot be interrupted once it has started.

Run with ``python examples/full_memory.py``.
"""

import time
from pathlib import Path

import matplotlib.pyplot as plt

from hp54645d import HP54645D, plot_capture

OUT = Path(__file__).parent / "captures"

with HP54645D("ASRL5::INSTR", baud_rate=19200) as scope:
    scope.stop()
    sizes = scope.memory_sizes(analog=[1], digital=[])
    points = sizes["ANALOG1"]
    print(f"channel 1 holds {points:,} points: about {points / 1920 / 60:.1f} min to read")

    started = time.monotonic()
    shown = [0.0]

    def progress(source: str, done: int, total: int) -> None:
        """Print a line every ten seconds."""
        if time.monotonic() - shown[0] > 10 or done == total:
            shown[0] = time.monotonic()
            print(f"  {source}: {done / total:.0%}")

    capture = scope.read_memory(analog=[1], digital=[], progress=progress)

print(f"read in {time.monotonic() - started:.0f} s")
saved = capture.save(OUT / f"memory_{capture.timestamp:%Y%m%d_%H%M%S}.npz")
print("saved", saved[0])

plot_capture(capture)      # zoom in with the toolbar: the envelope turns into samples
plt.show()
