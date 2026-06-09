import tempfile
from pathlib import Path

from killswitch import KillSwitch
from state_manager import StateManager


def test_killswitch_blocks_when_daily_loss_hit():
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp)
        sm = StateManager(state_dir=sd)
        sm.update(today_realized_pnl_usd=-10.0, today_trade_count=2)
        ks = KillSwitch(state_manager=sm)
        decision = ks.check()
        assert decision.allowed is False
        assert "daily loss" in decision.reason.lower()


def test_killswitch_allows_normal_trade():
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp)
        sm = StateManager(state_dir=sd)
        sm.update(today_realized_pnl_usd=-2.0, today_trade_count=1,
                  today_consecutive_failures=0, halted=False,
                  smoke_test_active=False)
        ks = KillSwitch(state_manager=sm)
        decision = ks.check()
        assert decision.allowed is True


def test_killswitch_blocks_on_three_consecutive_failures():
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp)
        sm = StateManager(state_dir=sd)
        sm.update(today_consecutive_failures=3, halted=False)
        ks = KillSwitch(state_manager=sm)
        decision = ks.check()
        assert decision.allowed is False
        assert "consecutive" in decision.reason.lower()


def test_killswitch_blocks_on_explicit_halt():
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp)
        sm = StateManager(state_dir=sd)
        sm.update(halted=True, halt_reason="manual stop")
        ks = KillSwitch(state_manager=sm)
        decision = ks.check()
        assert decision.allowed is False
        assert "manual stop" in decision.reason
