from bot.signal_filter import evaluate_market


def test_filter_rejects_out_of_band():
    m = {
        "question": "NBA: Will the Lakers win?",
        "best_ask": 0.20,
        "liquidity_usd": 5000.0,
        "end_date_utc": "2026-06-12T00:00:00Z",
        "category": "sports",
    }
    decision = evaluate_market(m, now_utc="2026-06-09T12:00:00Z")
    assert decision.accepted is False
    assert "band" in decision.reason.lower()


def test_filter_rejects_low_liquidity():
    m = {
        "question": "NBA: Lakers vs Celtics",
        "best_ask": 0.45,
        "liquidity_usd": 500.0,
        "end_date_utc": "2026-06-12T00:00:00Z",
        "category": "sports",
    }
    decision = evaluate_market(m, now_utc="2026-06-09T12:00:00Z")
    assert decision.accepted is False
    assert "liquidity" in decision.reason.lower()


def test_filter_rejects_too_close_to_resolution():
    m = {
        "question": "Lakers vs Celtics",
        "best_ask": 0.45,
        "liquidity_usd": 5000.0,
        "end_date_utc": "2026-06-09T14:00:00Z",
        "category": "sports",
    }
    decision = evaluate_market(m, now_utc="2026-06-09T12:00:00Z")
    assert decision.accepted is False


def test_filter_rejects_too_far_to_resolution():
    m = {
        "question": "Lakers vs Celtics",
        "best_ask": 0.45,
        "liquidity_usd": 5000.0,
        "end_date_utc": "2026-07-01T00:00:00Z",
        "category": "sports",
    }
    decision = evaluate_market(m, now_utc="2026-06-09T12:00:00Z")
    assert decision.accepted is False


def test_filter_rejects_blacklist_keyword():
    m = {
        "question": "Esports tournament NBA winner",
        "best_ask": 0.45,
        "liquidity_usd": 5000.0,
        "end_date_utc": "2026-06-12T00:00:00Z",
        "category": "sports",
    }
    decision = evaluate_market(m, now_utc="2026-06-09T12:00:00Z")
    assert decision.accepted is False
    assert "blacklist" in decision.reason.lower()


def test_filter_accepts_clean_market():
    m = {
        "question": "NFL: Will the Chiefs beat the Bills?",
        "best_ask": 0.50,
        "liquidity_usd": 5000.0,
        "end_date_utc": "2026-06-12T00:00:00Z",
        "category": "sports",
    }
    decision = evaluate_market(m, now_utc="2026-06-09T12:00:00Z")
    assert decision.accepted is True
    assert decision.size_usd == 2.50
