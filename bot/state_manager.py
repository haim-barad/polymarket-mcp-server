"""State manager: atomic JSON file + SQLite trade ledger."""
from __future__ import annotations
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Optional

_DEFAULT_STATE: dict[str, Any] = {
    "halted": False,
    "halt_reason": None,
    "smoke_test_active": True,
    "smoke_test_lift_at_utc": None,
    "today_utc_date": None,
    "today_realized_pnl_usd": 0.0,
    "today_trade_count": 0,
    "today_consecutive_failures": 0,
    "last_trade_at_utc": None,
    "open_position_count": 0,
    "open_exposure_usd": 0.0,
    "onchain_positions_by_market": {},
    "cap_alert_sent_tick": False,
    "cap_alert_sent_today": False,
    "pre_trade_cap_logged_today": False,
}


class StateManager:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.state_dir / "state.json"
        self.db_file = self.state_dir / "bot.db"
        self._lock = Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    condition_id TEXT,
                    token_id TEXT,
                    side TEXT,
                    price REAL,
                    size_usd REAL,
                    status TEXT,
                    order_id TEXT,
                    fill_ts_utc TEXT,
                    settle_ts_utc TEXT,
                    pnl_usd REAL
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS strategy_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_utc TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    event_title TEXT,
                    action TEXT,
                    detail_json TEXT
                )
            """)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_file, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def read(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return dict(_DEFAULT_STATE)
        try:
            with self.state_file.open() as f:
                loaded = json.load(f)
            merged = dict(_DEFAULT_STATE)
            merged.update(loaded)
            return merged
        except (json.JSONDecodeError, OSError):
            return dict(_DEFAULT_STATE)

    def update(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            current = self.read()
            current.update(kwargs)
            current["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
            self._atomic_write(current)
            return current

    def _atomic_write(self, data: dict[str, Any]) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.state_dir, prefix=".state.", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.state_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def set_halt(self, halted: bool, reason: Optional[str] = None) -> None:
        self.update(halted=halted, halt_reason=reason)

    def record_trade(
        self, *, condition_id: str, token_id: str, side: str,
        price: float, size_usd: float, status: str, order_id: str,
    ) -> int:
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO trades
                   (ts_utc, condition_id, token_id, side, price, size_usd,
                    status, order_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    condition_id, token_id, side, price, size_usd,
                    status, order_id,
                ),
            )
            return cur.lastrowid

    def trades_today(self) -> list[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as con:
            rows = con.execute(
                """SELECT * FROM trades
                   WHERE substr(ts_utc, 1, 10) = ?
                   ORDER BY id DESC""",
                (today,),
            ).fetchall()
        return [dict(r) for r in rows]

    def record_strategy_event(self, *, strategy: str, event_title: str,
                              action: str, detail: Optional[dict] = None) -> int:
        """Log a strategy-level event (arb found, arb executed, daily digest,
        etc.) for later analysis."""
        with self._connect() as con:
            cur = con.execute(
                """INSERT INTO strategy_events
                   (ts_utc, strategy, event_title, action, detail_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    strategy,
                    (event_title or "")[:200],
                    action,
                    json.dumps(detail or {}),
                ),
            )
            return cur.lastrowid

    def realized_pnl_today(self) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as con:
            row = con.execute(
                """SELECT COALESCE(SUM(pnl_usd), 0) AS pnl
                   FROM trades
                   WHERE substr(ts_utc, 1, 10) = ?
                     AND status = 'SETTLED'""",
                (today,),
            ).fetchone()
        return float(row["pnl"] or 0.0)
