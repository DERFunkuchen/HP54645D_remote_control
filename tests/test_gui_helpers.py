# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""The GUI's number handling. No window is opened."""

from __future__ import annotations

import pytest

from hp54645d.gui import format_duration, format_si, parse_si, transfer_seconds


@pytest.mark.parametrize(
    ("text", "value"),
    [("200u", 200e-6), ("200us", 200e-6), ("2m", 2e-3), ("1.5", 1.5), ("-1m", -1e-3),
     ("2.5V", 2.5), ("10k", 10e3), ("5e-8", 5e-8), ("50n", 50e-9)],
)
def test_parse_si(text: str, value: float) -> None:
    assert parse_si(text) == pytest.approx(value)


@pytest.mark.parametrize("value", [200e-6, 2.5625, 0.0, -1e-3, 16.0, 50e-9, 1.4])
def test_format_then_parse_round_trips(value: float) -> None:
    """A value read from the scope must not look changed on the next Apply."""
    assert parse_si(format_si(value)) == pytest.approx(value, rel=1e-6)


def test_the_transfer_estimate_matches_the_bench() -> None:
    """19200 baud carries 1920 bytes/s; 1940 was measured on the bench."""
    assert transfer_seconds(1_000_000, 19200) == pytest.approx(520.8, abs=1)
    assert format_duration(520.8) == "8 min 41 s"
    assert format_duration(42) == "42 s"
