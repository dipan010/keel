#!/usr/bin/env bash
# Advertise a GPU that does not exist.
#
# Kubernetes lets a node's status.capacity carry arbitrary extended resource
# names, and the kubelet does not verify the hardware behind them -- it is the
# same mechanism a real device plugin uses. So a node can claim nvidia.com/gpu
# and the scheduler will honour requests for it.
#
# WHAT THIS VALIDATES:  placement. nodeSelector, taint toleration, resource
#                       limits, ResourceQuota on requests.nvidia.com/gpu, and
#                       the unschedulable failure path.
# WHAT IT DOES NOT:     anything that runs on a GPU. No model is accelerated,
#                       no CUDA exists. Do not read a green suite as evidence
#                       that GPU serving works.
#
# Re-runnable: advertised capacity is lost if the node re-registers, so run
# this again after a cluster or node restart.
set -euo pipefail

CLUSTER=${CLUSTER:-keel}
NODE=${NODE:-k3d-${CLUSTER}-gpu-0}
GPUS=${GPUS:-4}
ACCELERATOR=${ACCELERATOR:-l4}

if ! kubectl get node "$NODE" >/dev/null 2>&1; then
  echo "creating node $NODE"
  k3d node create "${NODE#k3d-${CLUSTER}-}" --cluster "$CLUSTER" --role agent
  kubectl wait --for=condition=Ready "node/$NODE" --timeout=120s
fi

echo "advertising nvidia.com/gpu=$GPUS on $NODE"
kubectl patch node "$NODE" --subresource=status --type=json \
  -p "[{\"op\":\"add\",\"path\":\"/status/capacity/nvidia.com~1gpu\",\"value\":\"$GPUS\"}]"

echo "labelling keel.io/accelerator=$ACCELERATOR"
kubectl label node "$NODE" "keel.io/accelerator=$ACCELERATOR" --overwrite

echo "tainting keel.io/gpu=true:NoSchedule"
kubectl taint node "$NODE" keel.io/gpu=true:NoSchedule --overwrite

# A node added after `make fake` has none of the imported images, and the
# resulting ImagePullBackOff looks nothing like a scheduling problem.
if docker image inspect keel/fake-runtime:dev >/dev/null 2>&1; then
  echo "importing keel/fake-runtime:dev into the cluster"
  k3d image import keel/fake-runtime:dev -c "$CLUSTER" >/dev/null
fi

kubectl get node "$NODE" -o jsonpath='{.status.capacity.nvidia\.com/gpu}{"\n"}' \
  | xargs -I{} echo "capacity now: {}"
