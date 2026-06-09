import sys
from pathlib import Path

# Add bot dir to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def test_config_loads_defaults():
    from config import BotConfig
    cfg = BotConfig.load()
    assert cfg.per_trade_cap_usd == 5.0
    assert cfg.total_open_exposure_usd == 30.0
    assert cfg.daily_loss_stop_usd == 10.0
    assert cfg.daily_trade_count_max == 5
    assert cfg.in_band_low == 0.30
    assert cfg.in_band_high == 0.70
    assert cfg.min_liquidity_usd == 1000.0
    assert cfg.telegram_alerts_enabled is True
    assert cfg.bot_metadata == "haim-barad-polymarket-bot"
    assert cfg.daily_summary_utc_hour == 18
