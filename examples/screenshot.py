"""Read what the HP 54645D is showing, plot it, save it. Changes nothing on the scope.

Run with ``python examples/screenshot.py``.
"""

from pathlib import Path

import matplotlib.pyplot as plt

from hp54645d import HP54645D, plot_capture

with HP54645D("ASRL5::INSTR", baud_rate=19200) as scope:
    capture = scope.read_screen()     # every displayed channel, analog and digital

for n, trace in capture.analog.items():
    print(f"ch{n}: {len(trace.volts)} points, {trace.volts.min():.3f} to {trace.volts.max():.3f} V")
for n, trace in capture.digital.items():
    print(f"D{n}: {len(trace.levels)} points, high {trace.levels.mean():.0%} of the time")

out = Path(__file__).parent / "captures" / f"screen_{capture.timestamp:%Y%m%d_%H%M%S}"
out.parent.mkdir(exist_ok=True)
plot_capture(capture).savefig(out.with_suffix(".png"), dpi=120, facecolor="#101418")
capture.save_csv(out.with_suffix(".csv"))
print(f"saved {out}.png / .csv")
plt.show()
