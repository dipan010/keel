# Bootstrap

Cluster prerequisites, applied once, before Keel.

| Component            | Why                                              | Phase |
|----------------------|--------------------------------------------------|-------|
| ingress-nginx        | The cluster's only Ingress, in front of LiteLLM  | 1     |
| NVIDIA GPU Operator  | Device plugin, drivers, DCGM metrics             | 1 (GPU node) |
| Prometheus           | vLLM and DCGM scraping                           | 1     |
| KEDA                 | Lane C scale-to-zero                             | 2     |
| Loki                 | Pod log tails in failure events                  | 2     |

Local development needs none of these: `make cluster` plus the fake runtime
covers the whole control path without a GPU.

## GPU nodes

Label and taint before anything schedules:

    kubectl label node <node> keel.io/accelerator=a100-80g
    kubectl taint node <node> keel.io/gpu=true:NoSchedule

The taint keeps everything but inference workloads off hardware billed by the
hour.
