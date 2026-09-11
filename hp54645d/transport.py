# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
r"""The line to the instrument: commands, queries, binary blocks, the error queue.

The only module that touches pyvisa. Everything above it speaks in command
strings and gets strings or bytes back, which is also what makes the rest of
the package testable against a fake session.

Line settings (programmer's guide §4-8): 1200, 2400, 9600 or 19200 baud,
8 data bits, no parity, 1 stop bit, DTR or XON/XOFF handshake. The scope
cannot report them and cannot negotiate, so they must match its front panel.
Messages end in ``\n`` both ways.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from hp54645d.errors import CommandError, ScopeError, ScopeTimeout

#: Baud rates the scope's RS-232 interface offers (§4-8).
BAUD_RATES = (1200, 2400, 9600, 19200)

#: Handshake names, as this package spells them.
FLOW_CONTROLS = ("xon_xoff", "dtr_dsr", "none")

_BITS_PER_BYTE_ON_THE_LINE = 10  # start + 8 data + stop


class Transport:
    """A session to the scope, serialised by a lock.

    Every public method is one complete exchange, held under the lock, so a
    GUI thread and a worker thread cannot interleave a query with someone
    else's answer.
    """

    def __init__(self, session: Any, *, baud_rate: int | None = None) -> None:
        """Wrap an open pyvisa session (or anything with the same methods).

        Args:
            session: An open resource: ``write``, ``query``, ``read_bytes``,
                ``read_raw``, ``close`` and a ``timeout`` in milliseconds.
            baud_rate: The line rate, if serial. Used to size the timeout for
                long binary transfers.
        """
        self._session = session
        self._baud_rate = baud_rate
        self._lock = threading.RLock()

    @classmethod
    def open(
        cls,
        address: str,
        *,
        baud_rate: int = 19200,
        flow_control: str = "xon_xoff",
        timeout_s: float = 10.0,
    ) -> Transport:
        """Open a VISA resource and apply the line settings.

        Args:
            address: VISA resource, e.g. ``"ASRL5::INSTR"`` for COM5, or
                ``"GPIB0::7::INSTR"`` through an HP-IB interface module.
            baud_rate: One of :data:`BAUD_RATES`. Ignored for non-serial
                resources.
            flow_control: One of :data:`FLOW_CONTROLS`. Ignored for
                non-serial resources.
            timeout_s: Default I/O timeout.

        Raises:
            ScopeError: If a setting is invalid or the resource cannot be
                opened.
        """
        if baud_rate not in BAUD_RATES:
            raise ScopeError(
                f"{baud_rate} baud is not offered by the scope; use one of {BAUD_RATES}"
            )
        if flow_control not in FLOW_CONTROLS:
            raise ScopeError(f"unknown flow control {flow_control!r}; use one of {FLOW_CONTROLS}")

        import pyvisa
        from pyvisa import constants

        try:
            session = pyvisa.ResourceManager().open_resource(address)
        except Exception as exc:
            if "Access is denied" in str(exc) or "PermissionError" in str(exc):
                raise ScopeError(
                    f"{address} is in use by another program --- only one can hold a serial "
                    f"port. Close the other one (another hp54645d-gui, a script, a terminal "
                    f"program) and try again."
                ) from exc
            raise ScopeError(f"cannot open {address!r}: {exc}") from exc

        session.read_termination = "\n"
        session.write_termination = "\n"
        session.timeout = round(timeout_s * 1000)
        is_serial = hasattr(session, "baud_rate")
        if is_serial:
            session.baud_rate = baud_rate
            session.data_bits = 8
            session.parity = constants.Parity.none
            session.stop_bits = constants.StopBits.one
            session.flow_control = {
                "xon_xoff": constants.ControlFlow.xon_xoff,
                "dtr_dsr": constants.ControlFlow.dtr_dsr,
                "none": constants.ControlFlow.none,
            }[flow_control]
        return cls(session, baud_rate=baud_rate if is_serial else None)

    def close(self) -> None:
        """Close the session. Safe to call twice; never raises."""
        with self._lock:
            if self._session is None:
                return
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
            self._session = None

    @property
    def is_open(self) -> bool:
        """Whether the session is open."""
        return self._session is not None

    # -- exchanges ----------------------------------------------------------

    def write(self, command: str) -> None:
        """Send one command. Does not check the error queue; see :meth:`check`."""
        with self._lock:
            self._call(command, lambda: self._require().write(command))

    def query(self, command: str) -> str:
        """Send a query and return the stripped answer."""
        with self._lock:
            return str(self._call(command, lambda: self._require().query(command))).strip()

    def query_block(
        self, command: str, progress: Callable[[int, int], None] | None = None
    ) -> bytes:
        r"""Send a query answered by an IEEE 488.2 definite-length block.

        ``#8``, eight digits of length, that many bytes, then ``\n`` ---
        e.g. ``#800002000`` and 2000 bytes (§8-15).

        Read **by length, never up to the terminator.** The data is binary and
        a byte of value 10 *is* ``\n``; reading to the terminator would cut
        the record short with no error.

        Read in chunks, each well inside the timeout, so a record of any size
        works --- a full-memory record of a million bytes takes nearly nine
        minutes at 19200 baud. **Once started, a block cannot be cancelled:**
        the scope sends all of it, whatever else it is sent meanwhile.

        With XON/XOFF, a data byte of 17 or 19 can be taken as flow control
        by the serial driver and lost. The length-exact read turns that into
        a timeout rather than a short record.

        Args:
            command: The query, e.g. ``":WAVeform:DATA?"``.
            progress: Called as ``progress(bytes_read, bytes_total)`` after
                each chunk.
        """
        with self._lock:
            session = self._require()
            self._call(command, lambda: session.write(command))
            start = self._read_exact(2, command)
            if start[:1] != b"#" or not start[1:2].isdigit():
                raise ScopeError(f"{command}: answer is not a binary block: {start!r}")
            length_field = self._read_exact(int(start[1:2]), command)
            if not length_field.isdigit():
                raise ScopeError(f"{command}: malformed block header {start + length_field!r}")
            length = int(length_field)
            body = bytearray()
            chunk = self._chunk_size(session)
            while len(body) < length:
                body += self._read_exact(min(chunk, length - len(body)), command)
                if progress is not None:
                    progress(len(body), length)
            self._read_exact(1, command)  # the \n that ends every answer
            return bytes(body)

    def errors(self) -> list[tuple[int, str]]:
        """Read and empty the error queue (`:SYSTem:ERRor?`, §6-18).

        Returns:
            ``(code, message)`` pairs, oldest first. Empty when there were
            none.
        """
        found: list[tuple[int, str]] = []
        with self._lock:
            for _ in range(30):  # the queue is finite; this only guards a loop
                code_text, _, message = self.query(":SYSTem:ERRor?").partition(",")
                code = int(code_text)
                if code == 0:
                    break
                found.append((code, message.strip().strip('"')))
        return found

    def check(self, context: str) -> None:
        """Raise if the error queue holds anything.

        Raises:
            CommandError: With every error the scope reported.
        """
        found = self.errors()
        if found:
            raise CommandError(context, found)

    def drain(self, quiet_s: float = 0.5) -> int:
        """Discard everything arriving until the line has been quiet a while.

        A query that timed out may still be answered later; left in the
        buffer, that answer would be taken as the reply to the next query and
        every answer after it would be off by one. Worse, a binary block keeps
        coming after its reader gave up --- the scope finishes sending it no
        matter what --- so this waits for **silence**, not for a newline a
        binary stream may never contain. After an abandoned full-memory
        transfer that can take minutes.

        Returns:
            How many bytes were thrown away.
        """
        with self._lock:
            session = self._require()
            if not hasattr(session, "bytes_in_buffer"):
                return 0
            discarded = 0
            quiet_since = time.monotonic()
            while time.monotonic() - quiet_since < quiet_s:
                time.sleep(0.05)
                waiting = session.bytes_in_buffer
                if waiting:
                    discarded += len(session.read_bytes(waiting))
                    quiet_since = time.monotonic()
            return discarded

    # -- internals ----------------------------------------------------------

    def _require(self) -> Any:
        if self._session is None:
            raise ScopeError("not connected")
        return self._session

    def _chunk_size(self, session: Any) -> int:
        """Bytes per read: at most 4096, and small enough to arrive in half a timeout."""
        if not self._baud_rate:
            return 4096
        bytes_per_s = self._baud_rate / _BITS_PER_BYTE_ON_THE_LINE
        return max(64, min(4096, int(bytes_per_s * session.timeout / 1000 / 2)))

    def _read_exact(self, count: int, command: str) -> bytes:
        session = self._require()
        return bytes(self._call(command, lambda: session.read_bytes(count)))

    def _call(self, command: str, action: Any) -> Any:
        try:
            return action()
        except ScopeError:
            raise
        except Exception as exc:
            if _is_timeout(exc):
                self.drain()
                raise ScopeTimeout(f"{command}: no answer from the scope") from exc
            raise ScopeError(f"{command}: {exc}") from exc


def _is_timeout(exc: Exception) -> bool:
    """Recognise a timeout from pyvisa, pyserial or a test double."""
    return isinstance(exc, TimeoutError) or "TMO" in str(getattr(exc, "abbreviation", ""))
