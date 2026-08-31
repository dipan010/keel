"""Reconcile decisions. Pure -- no cluster, no database."""

from __future__ import annotations

from control.domain.states import Status
from control.reconciler.diff import assess, compare_replicas_for, drift, material, orphans
from control.render import manifests

BASE = {
    "id": "d-1",
    "team_slug": "t",
    "lane": "b",
    "mode": "self_hosted",
    "replicas_min": 1,
    "replicas_max": 2,
    "accelerator": "cpu",
    "gpu_count": 0,
    "weights_uri": None,
    "engine_args": {},
}


def rendered(**over):
    return next(o for o in manifests.render({**BASE, **over}) if o["kind"] == "Deployment")


def live(*, ready=1, replicas=1, image=None, args=None, **over):
    obj = rendered(**over)
    obj["spec"]["replicas"] = replicas
    if image:
        obj["spec"]["template"]["spec"]["containers"][0]["image"] = image
    if args is not None:
        obj["spec"]["template"]["spec"]["containers"][0]["args"] = args
    obj["status"] = {"readyReplicas": ready}
    return obj


def row(**over):
    return {**BASE, "status": "ready", **over}


def test_healthy_deployment_stays_ready():
    a = assess(row(), live(), rendered())
    assert a.status is Status.READY
    assert not a.enqueue and not a.alert


def test_vanished_workload_is_degraded_and_requeued():
    a = assess(row(), None, rendered())
    assert a.status is Status.DEGRADED
    assert a.reason_code == "workload_vanished"
    assert a.enqueue and a.alert


def test_zero_ready_on_lane_b_is_degraded():
    a = assess(row(), live(ready=0, replicas=1), rendered())
    assert a.status is Status.DEGRADED
    assert a.reason_code == "no_ready_replicas"
    assert a.alert
    # The pods exist; blindly restarting them is not obviously right.
    assert not a.enqueue


def test_zero_ready_on_lane_c_is_the_expected_state():
    a = assess(row(lane="c"), live(ready=0, replicas=0, lane="c"), rendered(lane="c"))
    assert a.status is Status.SCALED_TO_ZERO
    assert not a.alert


def test_lane_c_replica_count_is_not_drift():
    # KEDA owns it. Comparing against the rendered 0 would report drift every
    # time the autoscaler did its job.
    assert compare_replicas_for.__module__  # sanity
    a = assess(row(lane="c"), live(ready=2, replicas=2, lane="c"), rendered(lane="c"))
    assert a.status is Status.READY, a.message


def test_manual_scale_on_lane_b_is_drift():
    a = assess(row(), live(ready=3, replicas=3), rendered())
    assert a.status is Status.UPDATING
    assert a.reason_code == "drift"
    assert "replicas" in a.drifted
    assert a.enqueue


def test_image_change_is_drift():
    a = assess(row(), live(image="someone/else:v1"), rendered())
    assert "image" in a.drifted
    assert a.detail["fields"]["image"]["got"] == "someone/else:v1"


def test_engine_flag_change_is_drift():
    a = assess(
        row(), live(args=["--model", "/models/weights", "--port", "8000", "--hacked"]), rendered()
    )
    assert "args" in a.drifted


def test_defaults_the_api_server_adds_are_not_drift():
    # The API server fills in dozens of fields we never set. A naive deep
    # compare reports drift on every pass, forever.
    obj = live()
    obj["spec"]["template"]["spec"]["dnsPolicy"] = "ClusterFirst"
    obj["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 30
    obj["spec"]["strategy"] = {"type": "RollingUpdate"}
    obj["metadata"]["annotations"] = {"deployment.kubernetes.io/revision": "1"}
    assert assess(row(), obj, rendered()).status is Status.READY


def test_recovery_moves_degraded_back_to_ready():
    a = assess(row(status="degraded"), live(), rendered())
    assert a.status is Status.READY
    assert a.message == "recovered"


def test_availability_is_reported_before_drift():
    # A workload that is down should read as down, not as configuration drift.
    a = assess(row(), live(ready=0, replicas=9, image="wrong:1"), rendered())
    assert a.reason_code == "no_ready_replicas"


def test_orphans_are_found_by_label():
    obj = {
        "metadata": {
            "name": "vllm-x",
            "namespace": "keel-inf-t",
            "labels": {"keel.io/deployment-id": "gone"},
        }
    }
    mine = {
        "metadata": {
            "name": "vllm-y",
            "namespace": "keel-inf-t",
            "labels": {"keel.io/deployment-id": "d-1"},
        }
    }
    found = orphans([obj, mine], {"d-1"})
    assert [o["metadata"]["name"] for o in found] == ["vllm-x"]


def test_unlabelled_objects_are_never_orphans():
    # Something without our label was never ours to reason about.
    assert orphans([{"metadata": {"name": "x", "labels": {}}}], set()) == []


def test_drift_helper_ignores_extra_actual_fields():
    assert drift({"a": 1}, {"a": 1, "b": 2}) == ()
    assert drift({"a": 1}, {"a": 2}) == ("a",)


def test_material_omits_replicas_when_asked():
    assert "replicas" not in material(rendered(), compare_replicas=False)
    assert "replicas" in material(rendered())
