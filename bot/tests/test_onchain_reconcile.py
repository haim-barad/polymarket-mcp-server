"""Tests for the on-chain reconciliation path added 2026-06-09."""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Make sure the bot package is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import bot.onchain_reconcile as oc
from bot.config import BotConfig
from bot.state_manager import StateManager


def test_reconcile_updates_state_with_onchain_truth():
    """reconcile() should pull positions from data-api, compute exposure,
    and persist open_position_count + open_exposure_usd into state.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(state_dir=Path(tmpdir))

        # Returns already-normalized (underscore) shape — same shape
        # fetch_positions() produces after normalizing the data-api response.
        fake_positions = [
            {
                "title": "Carolina Hurricanes win Stanley Cup?",
                "condition_id": "0xabc",
                "outcome": "Yes",
                "size": 32.85,
                "current_value": 12.32,
                "initial_value": 12.48,
                "cash_pnl": -0.16,
            },
            {
                "title": "Argentina win World Cup?",
                "condition_id": "0xdef",
                "outcome": "Yes",
                "size": 111.11,
                "current_value": 9.39,
                "initial_value": 10.00,
                "cash_pnl": -0.61,
            },
        ]

        with patch.object(oc, "fetch_positions", return_value=fake_positions):
            cfg = BotConfig.load()
            summary = oc.reconcile(sm, "0xWALLET", config=cfg)

        assert summary["open_position_count"] == 2
        assert abs(summary["open_exposure_usd"] - 21.71) < 0.01
        assert "0xabc" in summary["positions_by_market"]
        assert "0xdef" in summary["positions_by_market"]
        assert not summary["over_exposure_limit"]  # 21.71 < 30

        s = sm.read()
        assert s["open_position_count"] == 2
        assert abs(s["open_exposure_usd"] - 21.71) < 0.01


def test_reconcile_flags_overexposure():
    """If on-chain exposure > cap, summary should flag it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(state_dir=Path(tmpdir))

        # 40 USD of fake exposure > 30 USD cap
        fake_positions = [
            {
                "title": "X",
                "condition_id": "0x1",
                "outcome": "Yes",
                "size": 100,
                "current_value": 40.0,
                "initial_value": 40.0,
                "cash_pnl": 0,
            }
        ]
        with patch.object(oc, "fetch_positions", return_value=fake_positions):
            cfg = BotConfig.load()
            summary = oc.reconcile(sm, "0xWALLET", config=cfg)

        assert summary["over_exposure_limit"] is True
        assert abs(summary["exceeded_by_usd"] - 10.0) < 0.01


def test_reconcile_handles_api_error_gracefully():
    """If data-api is down, reconcile should return empty summary
    without crashing the bot."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(state_dir=Path(tmpdir))
        with patch.object(oc, "fetch_positions", return_value=[]):  # empty = failure
            cfg = BotConfig.load()
            summary = oc.reconcile(sm, "0xWALLET", config=cfg)

        assert summary["open_position_count"] == 0
        assert summary["open_exposure_usd"] == 0
        assert summary["over_exposure_limit"] is False


def test_killswitch_blocks_when_onchain_exposure_exceeds_cap():
    """The kill switch must respect the on-chain exposure in state, not
    just the local trade ledger. This is the fix for the 2026-06-09
    duplicate-trades incident."""
    from bot.killswitch import KillSwitch
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(state_dir=Path(tmpdir))
        # Simulate that an earlier run left 35 USD of on-chain exposure
        sm.update(open_exposure_usd=35.0)
        ks = KillSwitch(state_manager=sm)
        d = ks.check()
        assert d.allowed is False
        assert "exposure" in d.reason.lower()
        assert "35" in d.reason


def test_killswitch_allows_trade_when_onchain_under_cap():
    """The on-chain check should not block when local exposure is 0 and
    the cap is high enough to accommodate one $2.50 trade."""
    from bot.killswitch import KillSwitch
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(state_dir=Path(tmpdir))
        sm.update(open_exposure_usd=0.0, today_trade_count=0)
        ks = KillSwitch(state_manager=sm)
        d = ks.check()
        assert d.allowed is True
