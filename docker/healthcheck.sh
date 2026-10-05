#!/bin/bash

PORT="${API_PORT:-9000}"

if [ -n "$TLS_CERTIFICATE_PATH" ] && [ -n "$TLS_PRIVATE_KEY_PATH" ]; then
  PROTO="https"
else
  PROTO="http"
fi

# -k: TLS certificate is likely not issued for 127.0.0.1, so don't verify
# fork hook (docs/ai/DEPLOY.md, "Backups and restores"): a backup restore answers every /api/ request, this one too,
# 503 paused_for_restore while it runs. The server is up, so that is healthy; any other error, or no answer, still fails.
response=$(curl -sk -w '\n%{http_code}' "${PROTO}://127.0.0.1:${PORT}/api/app/about") || exit 1
status="${response##*$'\n'}"
[ "$status" -lt 400 ] || { [ "$status" = 503 ] && [[ "$response" == *'"paused_for_restore"'* ]]; }
