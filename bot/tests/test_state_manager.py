import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile


def test_state_file_atomic_write_and_read():
    from state_manager import StateManager
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        sm.update(halted=False, today_realized_pnl_usd=0.0, today_trade_count=0)
        sm.set_halt(True, reason="test")
        loaded = sm.read()
        assert loaded["halted"] is True
        assert loaded["halt_reason"] == "test"


def test_state_file_recovers_from_corruption():
    from state_manager import StateManager
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp)
        (sd / "state.json").write_text("{ corrupt json")
        sm = StateManager(state_dir=sd)
        loaded = sm.read()
        assert loaded["halted"] is False
        assert loaded["today_trade_count"] == 0


def test_trades_today_filters_by_utc_date():
    from state_manager import StateManager
    from datetime import datetime, timezone
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(state_dir=Path(tmp))
        sm.record_trade(
            condition_id="0xabc", token_id="t1", side="BUY",
            price=0.5, size_usd=2.5, status="OPEN", order_id="o1",
        )
        rows = sm.trades_today()
        assert len(rows) == 1
        assert rows[0]["order_id"] == "o1"
