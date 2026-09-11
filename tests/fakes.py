# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""A simulated serial line to the scope, built from answers the real one gave.

Every answer in ``REAL_ANSWERS`` was read from a 54645D, firmware A.02.08, on
2026-09-11. The behaviours are the real ones too: a waveform query for a
source that is not displayed gets no answer at all (a timeout, and a -221 in
the error queue), and binary blocks end in a newline.
"""

from __future__ import annotations

REAL_ANSWERS = {
    "*IDN?": "HEWLETT-PACKARD,54645D,0,A.02.08",
    ":ANALog1:RANGe?": "+1.60000E+01",
    ":ANALog1:OFFSet?": "+2.56250E+00",
    ":ANALog1:COUPling?": "DC",
    ":ANALog1:PROBe?": "X10",
    ":ANALog1:BWLimit?": "OFF",
    ":ANALog1:INVert?": "OFF",
    ":ANALog2:RANGe?": "+8.00000E-01",
    ":ANALog2:OFFSet?": "+0.00000E+00",
    ":ANALog2:COUPling?": "DC",
    ":ANALog2:PROBe?": "X1",
    ":ANALog2:BWLimit?": "OFF",
    ":ANALog2:INVert?": "OFF",
    ":TIMebase:MODE?": "MAIN",
    ":TIMebase:RANGe?": "+2.00000E-03",
    ":TIMebase:DELay?": "+0.00000000000000E+000",
    ":TIMebase:REFerence?": "CENT",
    ":TRIGger:MODE?": "NORM,EDGE",
    ":TRIGger:EDGE:SOURce?": "ANAL1",
    ":TRIGger:EDGE:LEVel?": "+2.56250E+00",
    ":TRIGger:EDGE:SLOPe?": "POS",
    ":TRIGger:COUPling?": "DC",
    ":TRIGger:REJect?": "OFF",
    ":TRIGger:HOLDoff?": "+2.00000E-07",
    ":TRIGger:NREJect?": "OFF",
    ":CHANnel:THReshold? POD1": "TTL,+1.40000E+00",
    ":CHANnel:THReshold? POD2": "TTL,+1.40000E+00",
    ":ACQuire:TYPE?": "NORM",
    ":ACQuire:COUNt?": "+1",
}

#: Four-point records with the real scaling: ch1 at 16 V / 2.5625 V offset,
#: 0.5 ms per point so four points span the 2 ms screen; pods as bytes.
PREAMBLES = {
    "ANALog1": "+0,+0,+4,+1,+5.00000E-04,-1.00000000000000E-003,+0,+6.25000E-02,+2.56250E+00,+128",
    "ANALog2": "+0,+0,+4,+1,+5.00000E-04,-1.00000000000000E-003,+0,+3.12500E-03,+0.00000E+00,+128",
    "POD1": "+0,+0,+4,+1,+5.00000E-04,-1.00000000000000E-003,+0,+1.00000E+00,+0.00000E+00,+0",
    "POD2": "+0,+0,+4,+1,+5.00000E-04,-1.00000000000000E-003,+0,+1.00000E+00,+0.00000E+00,+0",
}

DATA = {
    # 87 and 168 are the probe-adjust square's low and high on the real scope.
    "ANALog1": bytes([87, 168, 87, 168]),
    "ANALog2": bytes([128, 128, 128, 128]),
    "POD1": bytes([0b01, 0b11, 0b10, 0b00]),   # D0 = 1,1,0,0   D1 = 0,1,1,0
    "POD2": bytes([0, 0, 0, 0]),
}


def block(data: bytes) -> bytes:
    """Encode as the scope does: ``#8``, eight-digit length, data, newline."""
    return b"#8" + f"{len(data):08d}".encode() + data + b"\n"


class FakeScope:
    """Stands in for the pyvisa session. Records everything sent."""

    def __init__(
        self,
        *,
        displayed: tuple[str, ...] = ("ANALog1",),
        answers: dict[str, str] | None = None,
        errors: list[tuple[int, str]] | None = None,
        trigger_events: list[str] | None = None,
    ) -> None:
        """Set up what the scope shows and will answer."""
        self.timeout = 10_000
        self.displayed = set(displayed)
        self.answers = {**REAL_ANSWERS, **(answers or {})}
        self.error_queue = list(errors or [])
        self.trigger_events = list(trigger_events or ["+1"])
        self.sent: list[str] = []
        self.source: str | None = None
        self._pending = bytearray()
        self.closed = False

    # -- the session interface --------------------------------------------

    def write(self, command: str) -> None:
        """Record a command and act on the few that matter to the tests."""
        self.sent.append(command)
        verb, _, argument = command.partition(" ")
        if verb == ":WAVeform:SOURce":
            self.source = argument
        elif verb == ":VIEW":
            self.displayed.add(argument)
        elif verb == ":BLANk":
            self.displayed.discard(argument)
        elif command == ":WAVeform:DATA?":
            self._pending += block(DATA[self.source])

    def query(self, command: str) -> str:
        """Answer from the table, or behave as the real scope does."""
        self.sent.append(command)
        if command == ":SYSTem:ERRor?":
            if self.error_queue:
                code, message = self.error_queue.pop(0)
                return f'{code:+d},"{message}"'
            return '+0,"No error"'
        if command == ":TER?":
            return self.trigger_events.pop(0) if len(self.trigger_events) > 1 \
                else self.trigger_events[0]
        if command.startswith(":STATus? "):
            return "ON" if command.split()[1] in self.displayed else "OFF"
        if command == ":WAVeform:PREamble?":
            if self.source not in self.displayed and not self.source.startswith("POD"):
                self.error_queue.append((-221, "Settings conflict"))
                raise TimeoutError("no answer: source not displayed")
            return PREAMBLES[self.source]
        return self.answers[command]

    def read_bytes(self, count: int) -> bytes:
        """Serve pending binary data, or time out like a port that ran dry."""
        if count > len(self._pending):
            raise TimeoutError(f"wanted {count} bytes, {len(self._pending)} arrived")
        chunk = bytes(self._pending[:count])
        del self._pending[:count]
        return chunk

    def read_raw(self) -> bytes:
        """Nothing is ever left over on this line."""
        raise TimeoutError("nothing pending")

    def close(self) -> None:
        """Close."""
        self.closed = True

    # -- for assertions ---------------------------------------------------

    @property
    def commands(self) -> list[str]:
        """Everything sent that was not a query."""
        return [c for c in self.sent if not c.endswith("?") and "? " not in c]
