#!/usr/bin/env bash
# Install a trusted git archive staged by the Hyper-V host. Run as root.
set -euo pipefail
umask 077
action=${1:?} release=${2:?} stage=${3:?}
[[ $action =~ ^(Install|Update|Policy|Connect|Disconnect)$ && $release =~ ^[0-9a-f]{40}$ && $EUID == 0 ]]
[[ $(uname -m) == x86_64 ]] || { echo 'The pinned Microsoft HCM build requires x86_64.' >&2; exit 1; }
base=/opt/knownfaces-home
config=/etc/knownfaces
install -d -m 700 "$base/releases" "$config"
exec 9>"$base/deploy.lock"
flock -x 9
if [[ $action != Install && ! -L $base/current ]]; then
  echo 'Install KnownFaces on this guest before running this action.' >&2
  exit 1
fi
compose() { docker compose --project-name knownfaces-home --env-file "$base/current/images.env" -f "$base/current/infra/home/compose.yaml" "$@"; }
if [[ $action == Policy ]]; then
  [[ $(cat "$base/current/commit") == "$release" ]]
  compose exec -T mcp python -c '
import sys
from knownfaces.remote_policy import RemotePolicy
old=RemotePolicy.load("/policy.json")
new=RemotePolicy.model_validate_json(sys.stdin.buffer.read())
assert not (new.manage_enabled or new.recognize_enabled or new.snapshots)
assert old.model_dump(exclude={"cameras","subjects"})==new.model_dump(exclude={"cameras","subjects"})
' < "$stage/policy.json"
  cp -p "$config/policy.json" "$config/previous.json"
  install -o 10001 -g 10001 -m 0400 "$stage/policy.json" "$config/pending.json"
  mv -f "$config/pending.json" "$config/policy.json"
  if ! compose up -d --no-deps --force-recreate --wait --wait-timeout 120 mcp; then
    mv -f "$config/previous.json" "$config/policy.json"
    compose up -d --no-deps --force-recreate --wait --wait-timeout 120 mcp
    exit 1
  fi
  exit
fi
if [[ $action == Disconnect ]]; then
  compose --profile listener rm -sf listener relay-proxy
  exit
fi
if [[ $action == Connect ]]; then
  # Stdin is a single entity-level Listen connection string supplied over SSH.
  [[ $(cat "$base/current/commit") == "$release" ]]
  python3 -c '
import json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlsplit
c=sys.stdin.read().strip()
p=dict(x.split("=",1) for x in c.split(";") if x)
u=urlsplit(p.get("Endpoint",""))
if set(p)!={"Endpoint","SharedAccessKeyName","SharedAccessKey","EntityPath"} or u.scheme!="sb" or not (u.hostname or "").endswith(".servicebus.windows.net") or p["EntityPath"]!="mcp" or p["SharedAccessKeyName"]!="home-listen" or not p["SharedAccessKey"]:
    sys.exit("Expected the dedicated mcp/home-listen rule")
with tempfile.NamedTemporaryFile(dir="/etc/knownfaces",delete=False,mode="w") as f:
    json.dump([c],f)
os.replace(f.name,"/etc/knownfaces/listener.json")'
  compose --profile listener up -d --wait --wait-timeout 120 relay-proxy
  compose --profile listener up -d --force-recreate --no-deps --wait --wait-timeout 120 listener
  exit
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  . /etc/os-release
  [[ $ID == ubuntu && $VERSION_ID == 24.04 ]] || { echo 'Install Docker with Compose v2 or use Ubuntu 24.04.' >&2; exit 1; }
  apt-get update -qq
  packages=(docker-compose-v2)
  command -v docker >/dev/null || packages+=(docker.io)
  DEBIAN_FRONTEND=noninteractive apt-get install --no-remove -y "${packages[@]}"
fi
systemctl start docker
tmp=$(mktemp -d "$base/releases/stage.XXXXXXXX")
trap 'rm -rf -- "$tmp"' EXIT
python3 - "$stage/source.tar" "$release" "$tmp" <<'PY'
import sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    if archive.pax_headers.get("comment") != sys.argv[2]:
        sys.exit("Archive has unexpected git commit")
    archive.extractall(sys.argv[3], filter="data")
PY
image="knownfaces-home:$release"
hcm="knownfaces-home-hcm:$release"
build() {
  local tag=$1; shift
  if docker image inspect "$tag" >/dev/null 2>&1; then
    [[ $(docker inspect -f '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$tag") == "$release" ]]
  else
    docker build --label "org.opencontainers.image.revision=$release" -t "$tag" "$@"
  fi
}
build "$image" "$tmp"
build "$hcm" -f "$tmp/infra/home/Dockerfile.hcm" "$tmp/infra/home"
if [[ $action == Install ]]; then
  docker run --rm -i --network none "$image" python -c 'import sys; from knownfaces.remote_policy import RemotePolicy; p=RemotePolicy.model_validate_json(sys.stdin.buffer.read()); assert not (p.manage_enabled or p.recognize_enabled); assert not p.resource.endswith(".invalid/mcp")' < "$stage/policy.json"
  if [[ -f $config/policy.json ]]; then
    cmp --silent "$config/policy.json" "$stage/policy.json" || { echo 'Installed policy differs. Use a separately reviewed policy change.' >&2; exit 1; }
  else
    install -o 10001 -g 10001 -m 0400 "$stage/policy.json" "$config/policy.json"
  fi
fi
[[ -s $config/policy.json ]]
printf 'KF_IMAGE=%s\nKF_HCM_IMAGE=%s\n' "$image" "$hcm" > "$tmp/images.env"
printf '%s\n' "$release" > "$tmp/commit"
dst="$base/releases/$release"
[[ -d $dst ]] || mv -T "$tmp" "$dst"
previous=$(readlink "$base/current" || true)
switch_to() { ln -sfn "$1" "$base/next"; mv -Tf "$base/next" "$base/current"; }
active=()
if [[ -L $base/current && -n $(compose --profile listener ps -q listener) ]]; then active=(--profile listener); fi
switch_to "$dst"
if ! compose "${active[@]}" up -d --wait --wait-timeout 180; then
  if [[ -d $previous && -f $previous/images.env ]]; then
    switch_to "$previous"
    compose "${active[@]}" up -d --wait --wait-timeout 180
  fi
  exit 1
fi
echo "Installed KnownFaces $release. Ports 8093 and 8094 are bound to guest loopback."
