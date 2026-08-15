#!/bin/bash
set -euo pipefail

CONFIG_DIR="${HERDR_CONFIG_DIR:-$HOME/.config/herdr-remote}"
CONFIG_FILE="$CONFIG_DIR/config.env"
SECRETS_FILE="$CONFIG_DIR/secrets.env"

[ -r "$CONFIG_FILE" ] || { echo "Missing config: $CONFIG_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
if [ -r "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
fi
set +a

case "${1:-}" in
  relay)
    : "${HERDR_UV_PATH:?HERDR_UV_PATH is required}"
    : "${HERDR_RELAY_DIR:?HERDR_RELAY_DIR is required}"
    exec "$HERDR_UV_PATH" run "$HERDR_RELAY_DIR/herdr_relay.py"
    ;;
  tunnel)
    : "${HERDR_CLOUDFLARED_PATH:?HERDR_CLOUDFLARED_PATH is required}"
    : "${HERDR_RELAY_PORT:?HERDR_RELAY_PORT is required}"
    if [ -n "${HERDR_TUNNEL_PROXY:-}" ]; then
      export HTTP_PROXY="$HERDR_TUNNEL_PROXY"
      export HTTPS_PROXY="$HERDR_TUNNEL_PROXY"
      export ALL_PROXY="$HERDR_TUNNEL_PROXY"
    fi
    case "${HERDR_TUNNEL_MODE:-none}" in
      named)
        : "${HERDR_TUNNEL_NAME:?HERDR_TUNNEL_NAME is required for a named tunnel}"
        : "${HERDR_TUNNEL_CONFIG:?HERDR_TUNNEL_CONFIG is required for a named tunnel}"
        exec "$HERDR_CLOUDFLARED_PATH" tunnel --protocol "${HERDR_TUNNEL_PROTOCOL:-http2}" \
          --config "$HERDR_TUNNEL_CONFIG" run "$HERDR_TUNNEL_NAME"
        ;;
      temp)
        exec "$HERDR_CLOUDFLARED_PATH" tunnel --protocol "${HERDR_TUNNEL_PROTOCOL:-http2}" \
          --url "http://127.0.0.1:$HERDR_RELAY_PORT"
        ;;
      *)
        echo "Tunnel disabled (HERDR_TUNNEL_MODE=${HERDR_TUNNEL_MODE:-none})" >&2
        exit 64
        ;;
    esac
    ;;
  *)
    echo "Usage: $0 relay|tunnel" >&2
    exit 64
    ;;
esac
