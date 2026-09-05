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
# k3d names a created node "k3d-<name>-0", so the two are derived from one
# value rather than guessed at separately. An earlier version stripped a prefix
# to recover the name and produced "k3d-gpu-0-0" on a cluster where the node
# did not already exist -- which is every cluster except the one it was written
# on.
NODE_NAME=${NODE_NAME:-keel-gpu}
NODE=${NODE:-k3d-${NODE_NAME}-0}
GPUS=${GPUS:-4}
ACCELERATOR=${ACCELERATOR:-l4}

if ! kubectl get node "$NODE" >/dev/null 2>&1; then
  echo "creating node $NODE"
  k3d node create "$NODE_NAME" --cluster "$CLUSTER" --role agent
  kubectl wait --for=condition=Ready "node/$NODE" --timeout=180s
fi

# Patch capacity, then WAIT FOR ALLOCATABLE.
#
# The scheduler reads allocatable, not capacity, and the kubelet is what copies
# one to the other on its next status sync. On a node that has just joined, the
# kubelet can also overwrite capacity entirely -- so a single patch appears to
# succeed, allocatable stays empty, and every GPU pod sits Pending with no
# obvious cause. Re-patch until it sticks.
echo "advertising nvidia.com/gpu=$GPUS on $NODE"
for attempt in $(seq 1 20); do
  kubectl patch node "$NODE" --subresource=status --type=json \
    -p "[{\"op\":\"add\",\"path\":\"/status/capacity/nvidia.com~1gpu\",\"value\":\"$GPUS\"}]" \
    >/dev/null 2>&1 || true
  got=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
  if [ "$got" = "$GPUS" ]; then
    echo "  allocatable settled after ${attempt} attempt(s)"
    break
  fi
  sleep 3
done
if [ "$got" != "$GPUS" ]; then
  echo "ERROR: allocatable nvidia.com/gpu is '$got', expected '$GPUS'" >&2
  exit 1
fi

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

echo "capacity=$(kubectl get node "$NODE" -o jsonpath='{.status.capacity.nvidia\.com/gpu}')" \
     "allocatable=$(kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}')"
