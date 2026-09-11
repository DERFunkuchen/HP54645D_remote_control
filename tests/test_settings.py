# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 DERFunkuchen (https://github.com/DERFunkuchen/HP54645D_remote_control)
"""Working out what to send: pure, no instrument."""

from __future__ import annotations

import copy

import pytest

from hp54645d.settings import Settings, changes


def base() -> Settings:
    s = Settings()
    s.analog[1].displayed = True
    s.analog[1].range_v = 16.0
    s.analog[1].offset_v = 2.5625
    s.trigger.level_v = 2.5625
    s.digital.thresholds = {1: ("TTL", 1.4), 2: ("TTL", 1.4)}
    return s


def edited(**edits) -> Settings:
    s = copy.deepcopy(base())
    for path, value in edits.items():
        target = s
        *parents, name = path.split("__")
        for part in parents:
            target = target[int(part)] if part.isdigit() else getattr(target, part)
        setattr(target, name, value)
    return s


def test_identical_settings_need_nothing() -> None:
    assert not changes(base(), base())
    assert changes(base(), base()).count == 0


def test_a_value_the_scope_rounded_is_not_a_change() -> None:
    """Six significant figures from the scope must not look like an edit."""
    assert not changes(base(), edited(analog__1__offset_v=2.56250001))


def test_a_real_change_is_reported_as_configure_arguments() -> None:
    diff = changes(base(), edited(analog__1__range_v=8.0, timebase__range_s=5e-3))
    assert diff.analog == {1: {"range_v": 8.0}}
    assert diff.timebase == {"range_s": 5e-3}
    assert diff.count == 2


def test_choices_are_compared_in_canonical_form() -> None:
    """'cent' is CENTER and 'dc' is DC: not changes."""
    assert not changes(base(), edited(timebase__reference="cent", analog__1__coupling="dc"))


def test_a_choice_that_does_not_exist_fails_before_anything_is_sent() -> None:
    with pytest.raises(ValueError, match="'MIDDLE' is not one of"):
        changes(base(), edited(timebase__reference="MIDDLE"))


def test_digital_display_changes_both_ways() -> None:
    current = base()
    current.digital.displayed = (0, 1)
    wanted = copy.deepcopy(current)
    wanted.digital.displayed = (1, 5)
    assert changes(current, wanted).digital_displayed == {0: False, 5: True}


def test_a_threshold_voltage_matters_only_for_a_user_threshold() -> None:
    current = base()
    wanted = copy.deepcopy(current)
    wanted.digital.thresholds[1] = ("TTL", 9.9)          # TTL fixes its own voltage
    assert not changes(current, wanted)
    wanted.digital.thresholds[1] = ("USERDEF", 2.0)
    assert changes(current, wanted).thresholds == {1: ("USERDEF", 2.0)}


def test_an_average_count_is_sent_only_if_the_scope_takes_it() -> None:
    """In NORMAL mode the scope reports a count of 1, which cannot be set."""
    current = edited(acquire__count=16)
    assert not changes(current, edited(acquire__count=1)).acquire
    assert changes(base(), edited(acquire__type="AVERAGE", acquire__count=16)).acquire == {
        "type": "AVERAGE", "count": 16}
