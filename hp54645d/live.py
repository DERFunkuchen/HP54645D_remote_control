# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Repeated screen readouts --- a live view --- over a slow line.

A frame is :meth:`~hp54645d.HP54645D.read_screen` without its settings read:
at 19200 baud the settings take about a second, the transfer of a 500-point
channel about 0.45 s. So the reader reads the settings once and reuses them,
and reads them again:

- every ``refresh_s`` seconds, so a front-panel change shows up;
- whenever a frame fails --- a channel hidden on the front panel makes its
  transfer time out --- and then retries that frame once;
- whenever it is handed fresh ones with :meth:`LiveReader.use`, e.g. right
  after the caller changed settings.

Stale settings can only mislabel the graticule: every frame carries its own
preamble, so its volts and times are always right.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from hp54645d.errors import ScopeError

if TYPE_CHECKING:
    from hp54645d.scope import HP54645D
    from hp54645d.settings import Settings
    from hp54645d.waveform import Capture


class LiveReader:
    """Frames of the scope's screen, one per :meth:`read`, or by iterating.

    Get one from :meth:`hp54645d.HP54645D.live`. Iterating never ends by
    itself; ``break`` out of the loop, or call :meth:`read` from your own.
    """

    def __init__(
        self,
        scope: HP54645D,
        analog: Iterable[int] | None = None,
        digital: Iterable[int] | None = None,
        *,
        points: int | None = 500,
        refresh_s: float = 10.0,
    ) -> None:
        """Set up; nothing is read until the first frame.

        Args:
            scope: The connected scope.
            analog: Analog channels. Default: whatever is displayed.
            digital: Logic channels. Default: whatever is displayed.
            points: Analog record length per frame; fewer is faster.
            refresh_s: How long to trust settings before reading them again.
        """
        self.scope = scope
        self.analog = None if analog is None else tuple(analog)
        self.digital = None if digital is None else tuple(digital)
        self.points = points
        self.refresh_s = refresh_s
        self.frames = 0
        self._settings: Settings | None = None
        self._settings_at = 0.0
        self._started = 0.0

    def use(self, settings: Settings) -> None:
        """Adopt settings just read elsewhere, e.g. after ``apply()``."""
        self._settings, self._settings_at = settings, time.monotonic()

    def read(self) -> Capture:
        """Read one frame.

        Raises:
            ScopeError: If the frame fails even with freshly read settings.
        """
        if not self._started:
            self._started = time.monotonic()
        stale = self._settings is None or time.monotonic() - self._settings_at > self.refresh_s
        if not stale:
            try:
                return self._count(self._frame(self._settings))
            except ScopeError:
                # The display changed since. The failed transfer left its
                # complaint (-221) in the error queue; clear it, or the retry's
                # own check would blame the retry.
                self.scope.errors()
        self.use(self.scope.read_settings())
        return self._count(self._frame(self._settings))

    def __iter__(self) -> Iterator[Capture]:
        """Yield frames for as long as the caller keeps asking."""
        while True:
            yield self.read()

    @property
    def seconds_per_frame(self) -> float:
        """Average time per frame since the first."""
        if not self.frames:
            return 0.0
        return (time.monotonic() - self._started) / self.frames

    def _frame(self, settings: Settings) -> Capture:
        return self.scope.read_screen(self.analog, self.digital, points=self.points,
                                      settings=settings)

    def _count(self, capture: Capture) -> Capture:
        self.frames += 1
        return capture
