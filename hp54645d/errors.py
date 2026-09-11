# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Exceptions. Everything this package raises derives from :class:`ScopeError`."""

from __future__ import annotations


class ScopeError(Exception):
    """Base class for every error raised by this package."""


class ScopeTimeout(ScopeError):
    """The scope did not answer in time.

    Usually one of: wrong address or baud rate, the interface in printer mode
    rather than *Connect to Computer*, or a query for a channel that is not
    displayed --- the scope does not answer those at all.
    """


class CommandError(ScopeError):
    """The scope reported errors in its error queue after a command.

    Attributes:
        errors: The ``(code, message)`` pairs read from the queue, oldest
            first, e.g. ``[(-221, "Settings conflict")]``.
    """

    def __init__(self, context: str, errors: list[tuple[int, str]]) -> None:
        """Build the message from what the scope reported."""
        self.errors = errors
        detail = "; ".join(f"{code} {message}" for code, message in errors)
        super().__init__(f"{context}: the scope reported {detail}")


class ApplyError(ScopeError):
    """Some of the settings passed to ``apply()`` were refused.

    Everything else was still sent, and the settings were read back
    afterwards, so the caller can see what the scope actually has.

    Attributes:
        failures: One line per refused part, with the scope's own message.
        settings: The settings read back after applying.
    """

    def __init__(self, failures: list[str], settings: object) -> None:
        """Keep what failed and what the scope ended up with."""
        self.failures = failures
        self.settings = settings
        super().__init__("some settings were refused: " + " | ".join(failures))


class TriggerTimeout(ScopeError):
    """An acquisition was armed but nothing triggered it in time.

    The acquisition has been stopped, so the scope is responsive again.
    """
