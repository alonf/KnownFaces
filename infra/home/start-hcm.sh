#!/bin/sh
set -eu
test -s /run/secrets/listener.json
rm -f /run/hcm-ready
./HybridConnectionManagerService >/dev/null 2>&1 &
service=$!
trap 'kill "$service" 2>/dev/null || true' TERM INT EXIT
for attempt in $(seq 1 30); do
  kill -0 "$service"
  if python -c "import socket; socket.create_connection(('127.0.0.1',5001),1).close()" 2>/dev/null; then
    if ! hcm list 2>/dev/null | python -c 'import json,sys; rows=json.load(sys.stdin); sys.exit(0 if len(rows)==1 and rows[0].get("Name")=="mcp" else 1)' 2>/dev/null; then
      hcm add-from-file --path /run/secrets/listener.json >/dev/null 2>&1
    fi
    touch /run/hcm-ready
    wait "$service"
    exit $?
  fi
  sleep 1
done
exit 1
