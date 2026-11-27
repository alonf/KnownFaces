#!/bin/sh
# Run in a disposable Caddy container with --network none and no published ports.
set -eu
export KNOWNFACES_HOST=localhost
caddy start --config /etc/caddy/Caddyfile --adapter caddyfile >/tmp/edge-test.log 2>&1
# Startup returns before the localhost TLS certificate is always ready.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if wget --no-check-certificate -S -O /dev/null https://localhost/ 2>&1 | awk '/HTTP\// {seen=1} END {exit !seen}'; then
        break
    fi
    sleep 1
done
for path in / /login /v1/people /v1/snapshots /v1/snapshots/x/image /v1/samples/x/crop /static/app.js /mcp/extra /mcp /.well-known/oauth-protected-resource /.well-known/oauth-protected-resource/mcp
do
    case "$path" in
        /mcp|/.well-known/oauth-protected-resource|/.well-known/oauth-protected-resource/mcp) expected=502 ;;
        *) expected=404 ;;
    esac
    # An allowed path reaches the absent relay. Other paths terminate at the edge.
    status=$(wget --no-check-certificate -S -O /dev/null "https://localhost$path" 2>&1 | awk '/HTTP\// {print $2; exit}')
    printf '%s %s\n' "$status" "$path"
    test "$status" = "$expected"
done
