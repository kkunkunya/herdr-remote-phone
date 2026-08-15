#!/bin/bash
set -u

LABEL_RELAY="com.herdr-remote.relay"
LABEL_TUNNEL="com.herdr-remote.tunnel"
DOMAIN="gui/$(id -u)"
CONFIG_DIR="${HERDR_CONFIG_DIR:-$HOME/.config/herdr-remote}"
CONFIG_FILE="$CONFIG_DIR/config.env"
SECRETS_FILE="$CONFIG_DIR/secrets.env"
LOG_DIR="$HOME/Library/Logs/herdr-remote"
LOG="$LOG_DIR/guard.log"
mkdir -p "$LOG_DIR"

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }
kickstart() {
  local label="$1" plist="$HOME/Library/LaunchAgents/$1.plist"
  if [ ! -f "$plist" ]; then
    log "$label unavailable: missing $plist (run relay/install-service.sh)"
    return 1
  fi
  if ! loaded "$label"; then
    launchctl bootstrap "$DOMAIN" "$plist" >> "$LOG" 2>&1 || return 1
  fi
  launchctl kickstart -k "$DOMAIN/$label" >> "$LOG" 2>&1
}

[ -r "$CONFIG_FILE" ] || { log "missing $CONFIG_FILE"; exit 1; }
set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
if [ -r "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  source "$SECRETS_FILE"
fi
set +a

PORT="${HERDR_RELAY_PORT:-8375}"
TOKEN="${HERDR_RELAY_TOKEN:-}"
if [ -n "${HERDR_TUNNEL_PROXY:-}" ]; then
  export HTTP_PROXY="$HERDR_TUNNEL_PROXY"
  export HTTPS_PROXY="$HERDR_TUNNEL_PROXY"
  export ALL_PROXY="$HERDR_TUNNEL_PROXY"
fi
HEALTH_URL="http://127.0.0.1:$PORT/healthz"
[ -z "$TOKEN" ] || HEALTH_URL="$HEALTH_URL?token=$TOKEN"

if ! curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
  log "relay unhealthy; kickstarting $LABEL_RELAY"
  kickstart "$LABEL_RELAY" || log "relay restart failed"
  exit 0
fi

if [ "${HERDR_TUNNEL_MODE:-none}" != "none" ]; then
  if ! loaded "$LABEL_TUNNEL"; then
    log "tunnel service missing; bootstrapping $LABEL_TUNNEL"
    kickstart "$LABEL_TUNNEL" || log "tunnel restart failed"
  elif ! launchctl print "$DOMAIN/$LABEL_TUNNEL" 2>/dev/null | grep -Eq 'state = running|pid = [0-9]+'; then
    log "tunnel service not running; kickstarting $LABEL_TUNNEL"
    kickstart "$LABEL_TUNNEL" || log "tunnel restart failed"
  elif [ "${HERDR_TUNNEL_MODE:-}" = "named" ] && [ -n "${HERDR_TUNNEL_NAME:-}" ] && \
       ! "${HERDR_CLOUDFLARED_PATH:-cloudflared}" tunnel info "$HERDR_TUNNEL_NAME" 2>/dev/null | grep -q 'CONNECTOR ID'; then
    log "tunnel has no active connector; kickstarting $LABEL_TUNNEL"
    kickstart "$LABEL_TUNNEL" || log "tunnel restart failed"
  fi
fi
