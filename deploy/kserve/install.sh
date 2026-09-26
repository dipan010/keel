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

echo "== waiting for every KServe webhook server"
# kserve.yaml starts three controllers, each serving its own admission webhook.
# Applying cluster resources before ALL of them are ready makes the apiserver
# fail to call the missing webhook. An earlier version waited only for the
# main controller: from zero, all twelve LLMInferenceServiceConfigs failed
# with "failed calling webhook ... llmisvc", and the script reported that as
# the one known error -- a label written for a different failure, swallowing
# this one. Only a clean-cluster run found it.
for d in kserve-controller-manager llmisvc-controller-manager kserve-localmodel-controller-manager; do
  kubectl -n kserve rollout status "deploy/$d" --timeout=400s
done

echo "== cluster resources"
# Exactly one failure is expected in v0.21.0, and it is matched by its specific
# text: an LLMInferenceServiceConfig whose template references a field
# (TrustRemoteCode) its own webhook's type lacks. LLM-only; the spike does not
# use it. ANY other error is unexpected and fails the install -- after retries,
# since webhook endpoints can lag their Deployment's rollout by a few seconds.
KNOWN="can't evaluate field TrustRemoteCode"
for attempt in 1 2 3 4 5 6; do
  out=$(kubectl apply --server-side --force-conflicts \
        -f "$REL/kserve-cluster-resources.yaml" 2>&1) && break
  unexpected=$(echo "$out" | grep -i error | grep -vF "$KNOWN" || true)
  [ -z "$unexpected" ] && break
  echo "   attempt $attempt: $(echo "$unexpected" | wc -l | tr -d ' ') unexpected error(s), retrying"
  sleep 10
done
unexpected=$(echo "$out" | grep -i error | grep -vF "$KNOWN" || true)
if [ -n "$unexpected" ]; then
  echo "UNEXPECTED errors applying cluster resources:" >&2
  echo "$unexpected" | cut -c1-240 >&2
  exit 1
fi
echo "$out" | grep -F "$KNOWN" | head -1 | cut -c1-160 | sed 's/^/   (known v0.21.0 defect, LLM-only) /'

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
