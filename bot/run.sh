#!/usr/bin/env bash
# Manual launch: starts the bot in the foreground.
# In production this is invoked by a launchd/systemd unit, not by hand.
set -euo pipefail

cd "$(dirname "$0")/.."
exec ./venv/bin/python -m bot.runner
