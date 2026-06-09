#!/usr/bin/env bash
# Production launch wrapper for the polymarket auto-trader.
#
# Invoked by launchd (~/Library/LaunchAgents/ai.polymarket.bot.plist).
# Sources the hermes env (for TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHANNEL)
# and the MCP env (for POLYGON_PRIVATE_KEY, POLYGON_ADDRESS, etc.), then
# starts the bot in the foreground.
#
# Logs go to ~/Library/Logs/polymarket-bot.{out,err}.log per the plist.

set -euo pipefail

# Resolve absolute paths
REPO="/Users/haimbarad/Documents/GitHub/haim-barad/polymarket-mcp-server"
HERMES_ENV="$HOME/.hermes/.env"
MCP_ENV="$REPO/.env"

# Sanity checks
[ -d "$REPO" ]            || { echo "[fatal] repo missing: $REPO" >&2; exit 1; }
[ -x "$REPO/venv/bin/python" ] || { echo "[fatal] venv missing" >&2; exit 1; }
[ -r "$HERMES_ENV" ]      || { echo "[fatal] hermes env missing: $HERMES_ENV" >&2; exit 1; }
[ -r "$MCP_ENV" ]         || { echo "[fatal] mcp env missing: $MCP_ENV" >&2; exit 1; }

# Export every KEY=VALUE line from both .env files. Same pattern the
# hermes gateway uses. Comments and blank lines are skipped.
export_env_file() {
    while IFS= read -r line; do
        # Strip leading whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        # Skip comments and blanks
        [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
        # Must contain '='
        [[ "$line" != *"="* ]] && continue
        # Export
        export "$line"
    done < "$1"
}

export_env_file "$HERMES_ENV"
export_env_file "$MCP_ENV"

# Drop into the repo so relative imports / state paths work
cd "$REPO"

# Echo a start line so the log shows when the bot came up
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] polymarket-bot starting (pid=$$)" >&2

# Run in foreground. launchd will supervise.
exec ./venv/bin/python -m bot.runner
