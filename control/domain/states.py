"""Deployment lifecycle. No Kubernetes, no database, no I/O.

This module is the testable core and the part that outlives any particular
substrate. Nothing here may import a cluster client or a driver -- see
`test_no_infra_imports` in tests/.
"""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    UPSTREAM = "upstream"
    SELF_HOSTED = "self_hosted"


class Lane(StrEnum):
    A = "a"  # upstream: a route to a hosted API, no workload of ours
    B = "b"  # dedicated: vLLM on a GPU, replicas >= 1
    C = "c"  # scale-to-zero: same as B plus KEDA


class Status(StrEnum):
    REQUESTED = "requested"
    VALIDATING = "validating"
    # 'provisioning' is deliberately split. Waiting for a node to accept the pod
    # and loading 140GB of weights fail for different reasons and take different
    # amounts of time; collapsing them gives the user a spinner that means either
    # "no capacity, this will never work" or "downloading, please wait".
    SCHEDULING = "scheduling"
    LOADING = "loading"
    READY = "ready"
    UPDATING = "updating"
    DEGRADED = "degraded"
    SCALED_TO_ZERO = "scaled_to_zero"
    DELETING = "deleting"
    DELETED = "deleted"
    FAILED = "failed"


#: Allowed transitions. Anything absent here is a bug, not a state to handle.
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.REQUESTED: frozenset({Status.VALIDATING, Status.FAILED, Status.DELETING}),
    Status.VALIDATING: frozenset({Status.SCHEDULING, Status.READY, Status.FAILED, Status.DELETING}),
    #                                              ^ upstream skips the workload states
    Status.SCHEDULING: frozenset({Status.LOADING, Status.FAILED, Status.DELETING}),
    Status.LOADING: frozenset({Status.READY, Status.FAILED, Status.DELETING}),
    Status.READY: frozenset(
        {Status.UPDATING, Status.DEGRADED, Status.SCALED_TO_ZERO, Status.DELETING}
    ),
    Status.UPDATING: frozenset({Status.READY, Status.DEGRADED, Status.FAILED, Status.DELETING}),
    Status.DEGRADED: frozenset({Status.READY, Status.UPDATING, Status.FAILED, Status.DELETING}),
    Status.SCALED_TO_ZERO: frozenset({Status.READY, Status.DEGRADED, Status.DELETING}),
    Status.DELETING: frozenset({Status.DELETED, Status.FAILED}),
    # Terminal. A retry creates a NEW record rather than resurrecting this one,
    # so the event history stays honest about what actually happened.
    Status.DELETED: frozenset(),
    Status.FAILED: frozenset({Status.DELETING}),
}

TERMINAL = frozenset({Status.DELETED})
LIVE = frozenset({Status.READY, Status.DEGRADED, Status.SCALED_TO_ZERO, Status.UPDATING})


class IllegalTransition(Exception):
    def __init__(self, frm: Status, to: Status) -> None:
        super().__init__(f"illegal transition {frm} -> {to}")
        self.frm, self.to = frm, to


def can_transition(frm: Status, to: Status) -> bool:
    return to in TRANSITIONS[frm]


def assert_transition(frm: Status, to: Status) -> None:
    if not can_transition(frm, to):
        raise IllegalTransition(frm, to)


def zero_replicas_is_expected(lane: Lane) -> bool:
    """Zero ready replicas is a fault for lane B and the steady state for lane C.

    Getting this backwards produces either a permanently alarming dashboard or a
    real outage nobody notices.
    """
    return lane is Lane.C
