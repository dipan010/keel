#!/usr/bin/env bash
# Turn a rented Ubuntu GPU box into a Keel-ready single-node cluster.
#
# Run this ON the rented machine, as root or with sudo. It is idempotent.
#
# UNTESTED ON REAL HARDWARE. Everything Keel has run against so far is a fake
# runtime on a node that only claims to have a GPU, so treat the first run as
# part of the experiment: read what it prints rather than assuming it worked.
#
#   curl -sfL <this file> | bash
#   # or: scp it over and run it
set -euo pipefail

ACCELERATOR=${ACCELERATOR:-l4}     # must match the catalog entry's accelerator
K3S_VERSION=${K3S_VERSION:-}       # empty = k3s stable

say() { printf '\n=== %s ===\n' "$*"; }

say "what hardware is this"
nvidia-smi --query-gpu=name,memory.total,compute_cap,driver_version --format=csv \
  || { echo "no nvidia-smi: this box has no usable GPU driver" >&2; exit 1; }

# Compute capability decides dtype. Below 8.0 there is no bfloat16, and vLLM
# will silently downcast rather than fail -- see ADR-0007.
CAP=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | head -1)
echo
if awk "BEGIN{exit !($CAP < 8.0)}"; then
  echo "NOTE: compute capability $CAP has NO bfloat16."
  echo "      The catalog entry must say dtype: float16, or vLLM will downcast"
  echo "      silently and you will be benchmarking different numerics."
else
  echo "compute capability $CAP supports bfloat16."
fi

say "container runtime"
if ! command -v docker >/dev/null; then
  curl -sfL https://get.docker.com | sh
fi

say "nvidia container toolkit"
# Without this the container gets no GPU, and the failure looks like a CUDA
# error from inside vLLM rather than a missing device.
if ! command -v nvidia-ctk >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
fi
nvidia-ctk runtime configure --runtime=containerd --set-as-default || true

say "k3s"
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - \
    --write-kubeconfig-mode 644
fi
until k3s kubectl get nodes >/dev/null 2>&1; do sleep 2; done
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
alias kubectl='k3s kubectl'

say "nvidia device plugin"
# This is what makes nvidia.com/gpu a real allocatable resource -- the thing
# deploy/kind/fake-gpu.sh has been faking all along.
k3s kubectl apply -f \
  https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.17.0/deployments/static/nvidia-device-plugin.yml

say "label and taint the node"
NODE=$(k3s kubectl get nodes -o jsonpath='{.items[0].metadata.name}')
k3s kubectl label  node "$NODE" "keel.io/accelerator=$ACCELERATOR" --overwrite
k3s kubectl taint  node "$NODE" keel.io/gpu=true:NoSchedule --overwrite

say "wait for the GPU to be allocatable"
for i in $(seq 1 40); do
  GOT=$(k3s kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)
  [ -n "${GOT:-}" ] && [ "$GOT" != "0" ] && break
  sleep 5
done
echo "allocatable nvidia.com/gpu = ${GOT:-<none>}"
if [ -z "${GOT:-}" ] || [ "$GOT" = "0" ]; then
  echo "FAILED: the device plugin did not advertise a GPU." >&2
  echo "  k3s kubectl -n kube-system logs -l name=nvidia-device-plugin-ds" >&2
  exit 1
fi

say "done"
cat <<NOTE
Node:         $NODE
Accelerator:  $ACCELERATOR   (catalog entries must match this label)
Compute cap:  $CAP

Copy the kubeconfig to wherever Keel runs, replacing 127.0.0.1 with this
machine's reachable address:

  scp root@<this-box>:/etc/rancher/k3s/k3s.yaml ./gpu.kubeconfig
  sed -i '' "s/127.0.0.1/<this-box>/" ./gpu.kubeconfig
  export KUBECONFIG=\$PWD/gpu.kubeconfig

Then do the MANUAL baseline first -- see docs/baseline.md. Once Keel has run,
the image and weights are cached and a clean cold number is gone.
NOTE
