#!/usr/bin/env bash
# KServe for Keel -- ADR-0008 spike. Idempotent.
#
# Every setting below was found necessary, not assumed. Without them:
#   namespace         kserve.yaml v0.21.0 references `kserve` but never creates
#                     it, so the controller fails to install at all
#   Standard mode     the release default is Serverless, which needs Knative
#   no ingress        the release default creates an Ingress per
#                     InferenceService -- exposing backends outside the gateway
#                     and breaking invariant I1 while the ClusterIP test passes
#   domainTemplate    the default joins name and namespace into one DNS label;
#                     Keel's full-UUID names overflow 63 characters and KServe
#                     refuses the resource, even with ingress disabled
set -euo pipefail

KSERVE_VERSION=${KSERVE_VERSION:-v0.21.0}
CERT_MANAGER_VERSION=${CERT_MANAGER_VERSION:-v1.21.2}
REL=https://github.com/kserve/kserve/releases/download/$KSERVE_VERSION

echo "== cert-manager $CERT_MANAGER_VERSION (KServe's webhooks need it)"
kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/$CERT_MANAGER_VERSION/cert-manager.yaml" >/dev/null
kubectl -n cert-manager rollout status deploy/cert-manager-webhook --timeout=300s

echo "== KServe $KSERVE_VERSION"
kubectl create namespace kserve --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply --server-side --force-conflicts -f "$REL/kserve-crds.yaml" >/dev/null
# --force-conflicts: the Keel settings patched below co-own fields in
# inferenceservice-config, so a re-run otherwise refuses to apply. The
# patch re-asserts them immediately afterwards.
kubectl apply --server-side --force-conflicts -f "$REL/kserve.yaml" >/dev/null
kubectl -n kserve rollout status deploy/kserve-controller-manager --timeout=400s

echo "== cluster resources"
# One LLMInferenceServiceConfig in v0.21.0 is rejected by KServe's own webhook
# (a template references a field the type does not have). It affects only the
# LLM CRD, which this spike does not use, so it is reported and not fatal.
if ! out=$(kubectl apply --server-side --force-conflicts -f "$REL/kserve-cluster-resources.yaml" 2>&1); then
  echo "$out" | grep -i error | cut -c1-200 | sed 's/^/   (known, LLM-only) /'
fi

echo "== configuring for Keel"
cfg=$(kubectl -n kserve get configmap inferenceservice-config -o json)
patch=$(python3 - "$cfg" <<'PY'
import json, sys
d = json.loads(sys.argv[1])["data"]
dep, ing = json.loads(d["deploy"]), json.loads(d["ingress"])
dep["defaultDeploymentMode"] = "Standard"
ing["disableIngressCreation"] = True
ing["domainTemplate"] = "{{ .Name }}.{{ .Namespace }}.{{ .IngressDomain }}"
ing["ingressDomain"] = "keel.internal"
print(json.dumps({"data": {"deploy": json.dumps(dep), "ingress": json.dumps(ing)}}))
PY
)
kubectl -n kserve patch configmap inferenceservice-config --type merge -p "$patch" >/dev/null
kubectl -n kserve rollout restart deploy/kserve-controller-manager >/dev/null
kubectl -n kserve rollout status deploy/kserve-controller-manager --timeout=300s

echo "== done"
