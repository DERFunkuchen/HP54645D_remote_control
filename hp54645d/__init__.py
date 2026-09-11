# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Remote control and waveform readout for the HP 54645D oscilloscope.

```python
from hp54645d import HP54645D, plot_capture

with HP54645D("ASRL5::INSTR", baud_rate=19200) as scope:
    capture = scope.read_screen()      # what the screen shows, nothing changed
plot_capture(capture)
```

See ``docs/library.md``.
"""

from hp54645d.analysis import Measurement, measure
from hp54645d.errors import (
    ApplyError,
    CommandError,
    ScopeError,
    ScopeTimeout,
    TriggerTimeout,
)
from hp54645d.live import LiveReader
from hp54645d.plot import plot_capture
from hp54645d.scope import HP54645D
from hp54645d.settings import Settings, changes
from hp54645d.waveform import AnalogTrace, Capture, DigitalTrace, load_capture

__version__ = "0.1.0"

__all__ = [
    "HP54645D",
    "AnalogTrace",
    "ApplyError",
    "Capture",
    "CommandError",
    "DigitalTrace",
    "LiveReader",
    "Measurement",
    "ScopeError",
    "ScopeTimeout",
    "Settings",
    "TriggerTimeout",
    "changes",
    "load_capture",
    "measure",
    "plot_capture",
]
