import itertools

import pytest

from control.domain.states import (
    TRANSITIONS,
    IllegalTransition,
    Lane,
    Status,
    assert_transition,
    can_transition,
    zero_replicas_is_expected,
)


def test_every_status_has_a_transition_entry():
    for s in Status:
        assert s in TRANSITIONS, f"{s} missing from TRANSITIONS"


def test_happy_path_self_hosted():
    path = [
        Status.REQUESTED,
        Status.VALIDATING,
        Status.SCHEDULING,
        Status.LOADING,
        Status.READY,
    ]
    for frm, to in itertools.pairwise(path):
        assert_transition(frm, to)


def test_upstream_skips_workload_states():
    assert can_transition(Status.VALIDATING, Status.READY)


def test_deleted_is_terminal():
    assert TRANSITIONS[Status.DELETED] == frozenset()


def test_failed_does_not_return_to_ready():
    # A retry creates a new record. Resurrecting one makes the event history lie.
    assert not can_transition(Status.FAILED, Status.READY)
    with pytest.raises(IllegalTransition):
        assert_transition(Status.FAILED, Status.READY)


def test_zero_replicas_only_expected_for_lane_c():
    assert zero_replicas_is_expected(Lane.C)
    assert not zero_replicas_is_expected(Lane.B)
    assert not zero_replicas_is_expected(Lane.A)
