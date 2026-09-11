"""Set the HP 54645D up for the probe-adjust signal and take one triggered capture.

Channel 1's probe on the probe-adjust terminal: a 0-5 V square, about 1.2 kHz.
Channel 2 and D0-D1 are captured too, from the same trigger, to show that
several channels can be taken at once; connect something to them to see more
than a flat line.

    python examples/acquire.py
"""

import matplotlib.pyplot as plt

from hp54645d import HP54645D, TriggerTimeout, plot_capture

with HP54645D("ASRL5::INSTR", baud_rate=19200) as scope:
    scope.configure_analog(1, range_v=16.0, offset_v=2.5, coupling="DC")   # 2 V/div
    scope.configure_timebase(range_s=2e-3, delay_s=0.0)                    # 200 us/div
    scope.configure_trigger(mode="NORMAL", source="ANALOG1", level_v=2.5, slope="POSITIVE")

    try:
        capture = scope.acquire(analog=[1, 2], digital=[0, 1], timeout_s=5)
    except TriggerTimeout as exc:
        raise SystemExit(f"{exc} -- is the probe on the probe-adjust terminal?") from None

    scope.run()   # acquire leaves the scope stopped on the capture; carry on running

trace = capture.analog[1]
print(f"ch1: {trace.volts.min():.2f} V to {trace.volts.max():.2f} V over "
      f"{(trace.time_s[-1] - trace.time_s[0]) * 1e3:.2f} ms")
plot_capture(capture)
plt.show()
