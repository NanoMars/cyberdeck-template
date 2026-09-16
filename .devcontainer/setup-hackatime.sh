#!/usr/bin/env bash
# Writes the Hackatime config so time tracking works the moment the editor
# opens, with nothing for the participant to paste.
#
# HACKATIME_API_KEY arrives as a GitHub Codespaces user secret, scoped to this
# repository. Runs on every start, so a rotated key repairs itself.
#
# Deliberately NOT a placeholder if the key is missing: the extension caches
# the first value that looks like a valid key and then ignores the real one.
set -euo pipefail

CFG="${WAKATIME_HOME:-$HOME}/.wakatime.cfg"

if [ -z "${HACKATIME_API_KEY:-}" ]; then
  echo "cyberdeck: HACKATIME_API_KEY is not set, so time tracking is off."
  echo "cyberdeck: open the program site and set your Codespace up again."
  exit 0
fi

cat > "$CFG" <<CFGEOF
[settings]
api_url = https://hackatime.hackclub.com/api/hackatime/v1
api_key = ${HACKATIME_API_KEY}
heartbeat_rate_limit_seconds = 30
CFGEOF
chmod 600 "$CFG"
echo "cyberdeck: time tracking is on."
