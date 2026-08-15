#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/herdr-remote"
CONFIG_FILE="$CONFIG_DIR/config.env"
SECRETS_FILE="$CONFIG_DIR/secrets.env"
WS_PORT="${HERDR_RELAY_PORT:-8375}"

RELAY_PID=""
TUNNEL_PID=""

cleanup() {
    echo ""
    echo "Shutting down..."
    [ -n "$TUNNEL_PID" ] && kill "$TUNNEL_PID" 2>/dev/null && wait "$TUNNEL_PID" 2>/dev/null
    [ -n "$RELAY_PID" ] && kill "$RELAY_PID" 2>/dev/null && wait "$RELAY_PID" 2>/dev/null
    echo "Done."
    exit 0
}

trap cleanup INT TERM EXIT

echo "herdr-remote relay"
echo ""

# Load config if available (allexport so child relay sees every var)
if [ -f "$CONFIG_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  if [ -f "$SECRETS_FILE" ]; then
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
  fi
  set +a
fi
WS_PORT="${HERDR_RELAY_PORT:-$WS_PORT}"
UV="${HERDR_UV_PATH:-$(command -v uv || true)}"
CLOUDFLARED="${HERDR_CLOUDFLARED_PATH:-$(command -v cloudflared || true)}"
[ -n "$UV" ] || { echo "Error: uv not found." >&2; exit 1; }

# 1. Start relay
echo "Starting relay on :$WS_PORT..."
"$UV" run "$SCRIPT_DIR/herdr_relay.py" &
RELAY_PID=$!
sleep 2

if ! kill -0 "$RELAY_PID" 2>/dev/null; then
    echo "Error: Relay failed to start. Check if port $WS_PORT is in use."
    echo "  lsof -iTCP:$WS_PORT"
    RELAY_PID=""
    exit 1
fi
echo "Relay running (pid $RELAY_PID)"

# 2. Start tunnel (if cloudflared available)
if [ -n "$CLOUDFLARED" ]; then
    TUNNEL_MODE="${HERDR_TUNNEL_MODE:-temp}"

    if [ "$TUNNEL_MODE" = "named" ] && [ -n "$HERDR_TUNNEL_NAME" ]; then
        echo "Starting named tunnel ($HERDR_TUNNEL_NAME)..."
        CF_CONFIG="$HOME/.cloudflared/config-herdr.yml"
        if [ -f "$CF_CONFIG" ]; then
            "$CLOUDFLARED" tunnel --protocol "${HERDR_TUNNEL_PROTOCOL:-http2}" --config "$CF_CONFIG" run "$HERDR_TUNNEL_NAME" &
            TUNNEL_PID=$!
        else
            echo "Warning: Tunnel config not found at $CF_CONFIG"
            echo "Run install-service.sh to configure the named tunnel."
            echo "Falling back to temp tunnel..."
            TUNNEL_MODE="temp"
        fi
    fi

    if [ "$TUNNEL_MODE" = "temp" ]; then
        echo "Starting temp tunnel..."
        "$CLOUDFLARED" tunnel --protocol "${HERDR_TUNNEL_PROTOCOL:-http2}" --url "http://127.0.0.1:$WS_PORT" 2>&1 &
        TUNNEL_PID=$!
        sleep 4

        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            echo "Warning: Tunnel failed to start. Relay still running locally."
            TUNNEL_PID=""
        else
            # Extract URL from cloudflared output
            TUNNEL_URL=$(grep -o 'https://[^ ]*\.trycloudflare\.com' /proc/$TUNNEL_PID/fd/1 2>/dev/null || true)
            # Fallback: check recent log output
            if [ -z "$TUNNEL_URL" ]; then
                sleep 2
                echo ""
                echo "Tunnel starting... URL will appear below:"
                echo "(If not visible, check: ps aux | grep cloudflared)"
            fi
        fi
    fi

    if [ "$TUNNEL_MODE" = "none" ]; then
        echo "Tunnel disabled (config: HERDR_TUNNEL_MODE=none)"
    fi
else
    echo "cloudflared not found — running local only."
    echo "Install: brew install cloudflared"
fi

echo ""
echo "Ready. Press Ctrl+C to stop."
echo ""

# Wait for relay (primary process)
wait "$RELAY_PID"
