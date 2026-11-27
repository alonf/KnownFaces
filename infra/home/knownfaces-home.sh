#!/usr/bin/env bash
# Run from a clean release checkout directly on an existing Ubuntu guest.
set -euo pipefail
umask 077
action=${1:-} release=${2:-}
if [[ ! $action =~ ^(Install|Update|Policy|Connect|Disconnect)$ || ! $release =~ ^[0-9a-f]{40}$ ]]; then
  echo 'Usage: bash infra/home/knownfaces-home.sh Install|Policy SHA policy.json; Update|Disconnect SHA; Connect SHA subscription group relay-namespace' >&2
  exit 2
fi
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
root=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
git -C "$root" archive --format=tar --output="$stage/source.tar" "$release"
tar -xf "$stage/source.tar" -C "$stage" infra/home/install.sh
if [[ $action == Install || $action == Policy ]]; then
  install -m 0600 "${3:?private policy file required}" "$stage/policy.json"
fi
cmd=(sudo -n bash "$stage/infra/home/install.sh" "$action" "$release" "$stage")
if [[ $action == Connect ]]; then
  az relay hyco authorization-rule keys list --subscription "${3:?subscription}" -g "${4:?group}" \
    --namespace-name "${5:?namespace}" --hybrid-connection-name mcp --name home-listen \
    --query primaryConnectionString --output tsv --only-show-errors | "${cmd[@]}"
else
  "${cmd[@]}"
fi
