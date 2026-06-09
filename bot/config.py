"""Bot configuration. Single source of truth for the locked config.

Edit values here, not in the MCP server .env, because the bot's daily-PnL
and smoke-test logic is the bot's responsibility — not the MCP server's.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional


# State dir: override with POLYMARKET_BOT_STATE_DIR env var, default to bot/.
_BOT_DIR = Path(__file__).resolve().parent
_STATE_DIR = Path(os.environ.get("POLYMARKET_BOT_STATE_DIR", str(_BOT_DIR / "state")))


@dataclass(frozen=True)
class BotConfig:
    # Risk rails (HARD caps — bot refuses to violate)
    per_trade_usd: float = 2.50
    per_trade_cap_usd: float = 5.00
    total_open_exposure_usd: float = 30.00
    daily_loss_stop_usd: float = 10.00
    weekly_soft_alert_usd: float = 20.00
    daily_trade_count_max: int = 5

    # Smoke test
    smoke_test_trade_cap: int = 1
    smoke_test_duration_hours: int = 24

    # Signal (option a — in-band mechanical)
    in_band_low: float = 0.30
    in_band_high: float = 0.70
    min_liquidity_usd: float = 1000.0
    time_to_resolution_min_hours: float = 6.0
    time_to_resolution_max_days: float = 28.0
    order_ttl_seconds: int = 600
    tick_interval_seconds: int = 300

    # Universe ramp
    enabled_categories: tuple = ("sports",)
    sports_whitelist: tuple = (
        "NFL", "NBA", "MLB", "NHL", "UFC",
        "Champions League", "Premier League", "La Liga",
        "Bundesliga", "Serie A",
    )
    sports_blacklist_keywords: tuple = (
        "esports", "college", "ncaa", "niche",
    )
    hightech_blacklist_keywords: tuple = (
        "anthropic", "mythos", "claude mythos", "singularity",
    )
    news_blacklist_keywords: tuple = ()

    # Telegram
    telegram_alerts_enabled: bool = True
    daily_summary_utc_hour: int = 18
    bot_metadata: str = "haim-barad-polymarket-bot"

    # Paths
    state_dir: Path = _STATE_DIR
    state_file: Path = _STATE_DIR / "state.json"
    db_file: Path = _STATE_DIR / "bot.db"
    log_file: Path = _STATE_DIR / "bot.log"

    # Kill switch
    consecutive_failures_halt: int = 3

    @classmethod
    def load(cls) -> "BotConfig":
        alerts = os.environ.get("BOT_TELEGRAM_ALERTS", "true").lower() in ("1", "true", "yes")
        return cls(telegram_alerts_enabled=alerts)


def get_state_dir() -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR
