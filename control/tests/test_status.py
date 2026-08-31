"""Failure classification. No cluster needed -- these are exactly the paths that
are hard to reproduce on demand and easy to get wrong."""

from __future__ import annotations

from control.provisioner.status import UNSCHEDULABLE_GRACE_SECONDS, Phase, classify


def pod(*, phase="Pending", conditions=None, init=None, containers=None):
    return {
        "metadata": {"name": "vllm-x-abc", "namespace": "keel-inf-t"},
        "status": {
            "phase": phase,
            "conditions": conditions or [],
            "initContainerStatuses": init or [],
            "containerStatuses": containers or [],
        },
    }


def waiting(reason, name="vllm"):
    return {"name": name, "state": {"waiting": {"reason": reason, "message": f"{reason} here"}}}


def test_no_pods_yet_is_scheduling():
    assert classify([]).phase is Phase.SCHEDULING


def test_running_but_not_ready_is_loading():
    out = classify([pod(phase="Running")])
    assert out.phase is Phase.LOADING
    assert "loading weights" in out.message


def test_ready_condition_wins():
    out = classify([pod(phase="Running", conditions=[{"type": "Ready", "status": "True"}])])
    assert out.phase is Phase.READY


def test_image_pull_failure_is_fatal():
    out = classify([pod(containers=[waiting("ImagePullBackOff")])])
    assert out.phase is Phase.FAILED
    assert out.reason_code == "image_pull_failed"


def test_crashloop_is_fatal():
    assert classify([pod(containers=[waiting("CrashLoopBackOff")])]).reason_code == "crashloop"


def test_oom_is_reported_with_advice():
    out = classify(
        [pod(containers=[{"name": "vllm", "state": {"terminated": {"reason": "OOMKilled"}}}])]
    )
    assert out.reason_code == "oom"
    assert "max-model-len" in out.message


def test_oom_is_caught_from_last_state_after_restart():
    # A restarted container shows OOM in lastState, not state -- missing this
    # reports a healthy crashlooping pod as merely "loading".
    out = classify(
        [
            pod(
                phase="Running",
                containers=[
                    {
                        "name": "vllm",
                        "state": {"running": {}},
                        "lastState": {"terminated": {"reason": "OOMKilled"}},
                    }
                ],
            )
        ]
    )
    assert out.reason_code == "oom"


def test_unschedulable_is_tolerated_briefly():
    p = pod(
        conditions=[
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/2 nodes match keel.io/accelerator=a100-80g",
            }
        ]
    )
    # Transient: a node may be draining, another pod terminating.
    assert classify([p], elapsed_seconds=5).phase is Phase.SCHEDULING
    out = classify([p], elapsed_seconds=UNSCHEDULABLE_GRACE_SECONDS + 1)
    assert out.phase is Phase.FAILED
    assert out.reason_code == "unschedulable"
    assert "accelerator" in out.message


def test_fatal_container_state_beats_running_phase():
    # A pod can be Running with a crashlooping container. Report the crash.
    out = classify([pod(phase="Running", containers=[waiting("CrashLoopBackOff")])])
    assert out.phase is Phase.FAILED


def test_init_container_failure_is_caught():
    out = classify([pod(init=[waiting("ErrImagePull", name="fetch-weights")])])
    assert out.reason_code == "image_pull_failed"
    assert out.detail["container"] == "fetch-weights"


def test_outcome_maps_to_a_real_status():
    assert classify([pod(phase="Running")]).status.value == "loading"
    assert classify([]).status.value == "scheduling"
