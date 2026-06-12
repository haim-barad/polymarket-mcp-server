"""
telegram_notify.py — Small wrapper around the Telegram Bot API for the polymarket-bot.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_HOME_CHANNEL from ~/.hermes/.env
(or the process env). Posts a Markdown-formatted message.

Usage:
    from telegram_notify import notify
    notify("Trade opened", "*BUY* 5 Up @ $0.45 on BTC June 9")
    notify("Daily loss limit hit", "Halting for the rest of the day.")
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# Token + chat_id are loaded from ~/.hermes/.env (or process env override)
HERMES_ENV = Path.home() / ".hermes" / ".env"


def _load_env() -> dict[str, str]:
    """Load TELEGRAM_BOT_TOKEN and TELEGRAM_HOME_CHANNEL from ~/.hermes/.env."""
    env: dict[str, str] = {}
    # Process env wins
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_NAME"):
        v = os.environ.get(k)
        if v:
            env[k] = v
    if "TELEGRAM_BOT_TOKEN" in env and "TELEGRAM_HOME_CHANNEL" in env:
        return env
    # Fall back to .env file
    if not HERMES_ENV.exists():
        return env
    with open(HERMES_ENV) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_NAME") and v:
                env.setdefault(k, v)
    return env


def _format(text: str) -> tuple[str, str]:
    """Return (chat_id, url-encoded text) for a simple message.

    Escapes Markdown-special characters (underscores, asterisks, brackets)
    in the text before sending, so snake_case identifiers in messages
    don't get interpreted as italic and cause HTTP 400 errors.
    """
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_HOME_CHANNEL", "")
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_HOME_CHANNEL not set. "
            "Check ~/.hermes/.env or run `hermes gateway setup telegram`."
        )
    # Escape Markdown special chars (other than those in our formatting).
    # The safest is to escape _, *, [, ], `, \ which are the Markdown
    # reserved characters in Telegram's Markdown mode.
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, "\\" + ch)
    return chat_id, urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    })


def notify(text: str, silent: bool = False) -> bool:
    """Post `text` to the configured Telegram chat. Returns True on success.

    Set silent=True to suppress notification sound (use for routine heartbeats).
    """
    chat_id, body = _format(text)
    env = _load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    if silent:
        # parse_mode still set, just disable_notification
        body = body + "&disable_notification=true"
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
        return bool(resp.get("ok"))
    except Exception as e:  # noqa: BLE001
        # Never crash the bot because Telegram was down
        print(f"[telegram] notify failed: {type(e).__name__}: {e}", flush=True)
        return False


if __name__ == "__main__":
    # Quick CLI test
    import sys
    msg = sys.argv[1] if len(sys.argv) > 1 else "Test message from polymarket-bot/telegram_notify.py"
    ok = notify(msg)
    print(f"sent={ok}")
    sys.exit(0 if ok else 1)
