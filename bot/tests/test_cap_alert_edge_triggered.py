"""
Tests for the edge-triggered cap-alert logic (added 2026-06-12).

Haim's spec: "should only appear once when it exceeds the cap and not
repeated until the next time it goes below the cap and then re-exceeds
the cap."

The previous implementation used level-triggered flags
(`cap_alert_sent_today`, `cap_alert_sent_tick`) that re-fired on every
day rollover or every tick. The new helper `_should_alert_over_cap`
in `bot/runner.py` is pure and edge-triggered: it fires only on the
rising edge of the over-cap condition.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bot.runner import _should_alert_over_cap


CAP = 50.0


# Rising edge: under cap → over cap → ALERT
def test_rising_edge_fires():
    alert, latched = _should_alert_over_cap(
        prev_exposure=45.0, curr_exposure=55.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True


def test_rising_edge_from_below_zero_fires():
    """Starting from 0 exposure, first over-cap event fires."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=0.0, curr_exposure=60.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True


def test_rising_edge_from_exactly_at_cap_fires():
    """At-cap is NOT over-cap. Crossing from at-cap to over-cap is a rising edge."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=50.0, curr_exposure=50.01, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True


# Continuous over-cap: do NOT re-fire
def test_continuous_over_cap_silent():
    """Already over cap from previous tick, still over cap → no alert."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=55.0, curr_exposure=56.0, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is True


def test_continuous_over_cap_oscillating_silent():
    """Over-cap exposure jitters around $50-$60 without ever dropping
    below the cap → only the first tick should have alerted."""
    # Stay continuously over cap (no dip below)
    exposures = [51.0, 55.0, 60.0, 52.0, 58.0]
    # First tick: rising edge (prev=0, curr=51) → fire + latch
    alert, latched = _should_alert_over_cap(
        prev_exposure=0.0, curr_exposure=exposures[0], cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True
    # Subsequent ticks: stays latched, never re-fires
    prev = exposures[0]
    for curr in exposures[1:]:
        alert, latched = _should_alert_over_cap(
            prev_exposure=prev, curr_exposure=curr, cap=CAP, latched=latched
        )
        assert alert is False, f"re-fired at exposure=${curr} after starting at $51"
        prev = curr


def test_dip_below_then_re_exceed_fires_again():
    """Haim's spec exactly: 'not repeated until the next time it goes
    below the cap and then re-exceeds the cap.' A dip below the cap
    re-arms the alert, so a subsequent over-cap event DOES re-fire."""
    # First over-cap episode: prev=0 → curr=51 → fire
    alert, latched = _should_alert_over_cap(
        prev_exposure=0.0, curr_exposure=51.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True
    # Stays over cap, jitters around 55
    for prev, curr in [(51.0, 55.0), (55.0, 60.0), (60.0, 52.0)]:
        alert, latched = _should_alert_over_cap(
            prev_exposure=prev, curr_exposure=curr, cap=CAP, latched=latched
        )
        assert alert is False
    # Drops under cap (50→49): falling edge, re-arm
    alert, latched = _should_alert_over_cap(
        prev_exposure=52.0, curr_exposure=49.0, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is False
    # Re-crosses over cap: RE-FIRE (this is exactly what Haim asked for)
    alert, latched = _should_alert_over_cap(
        prev_exposure=49.0, curr_exposure=60.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True


# Falling edge: over cap → under cap → no alert, re-arm
def test_falling_edge_clears_latch_no_alert():
    alert, latched = _should_alert_over_cap(
        prev_exposure=55.0, curr_exposure=45.0, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is False


def test_falling_edge_to_exactly_at_cap_clears_latch():
    """Going from 50.01 (over) to 50.00 (at cap) is a falling edge."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=50.01, curr_exposure=50.00, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is False


# Re-arm: under cap → over cap → ALERT (after a previous over-cap episode)
def test_rearm_fires_again_on_new_rising_edge():
    """The full Haim scenario: over cap (alerted), comes back under, then
    goes over again. The second over-cap event should re-alert."""
    # First over-cap episode
    alert, latched = _should_alert_over_cap(
        prev_exposure=45.0, curr_exposure=55.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True
    # Continuous over-cap: silent
    alert, latched = _should_alert_over_cap(
        prev_exposure=55.0, curr_exposure=58.0, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is True
    # Falling edge: re-arm
    alert, latched = _should_alert_over_cap(
        prev_exposure=58.0, curr_exposure=40.0, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is False
    # New rising edge: RE-FIRE
    alert, latched = _should_alert_over_cap(
        prev_exposure=40.0, curr_exposure=55.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True


# Never over cap: nothing fires
def test_stays_under_cap_silent():
    alert, latched = _should_alert_over_cap(
        prev_exposure=10.0, curr_exposure=20.0, cap=CAP, latched=False
    )
    assert alert is False
    assert latched is False


def test_stays_under_cap_at_exact_cap_silent():
    """Sitting at exactly the cap is NOT over-cap."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=50.0, curr_exposure=50.0, cap=CAP, latched=False
    )
    assert alert is False
    assert latched is False


# None prev_exposure handling
def test_none_prev_exposure_treated_as_zero():
    """First tick ever (no prior state) should fire on over-cap."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=None, curr_exposure=55.0, cap=CAP, latched=False
    )
    assert alert is True
    assert latched is True


# Stale latch: was latched, comes back under, no rising edge yet
def test_stale_latch_does_not_fire_on_still_under():
    """Edge case: latched=True (from before) but curr is now under cap
    with no observed transition. The function should still detect the
    falling edge and re-arm, but NOT fire an alert (we're not at a
    rising edge of the over-cap condition)."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=60.0, curr_exposure=30.0, cap=CAP, latched=True
    )
    assert alert is False
    assert latched is False


# Defensive: latched=True, prev=under, curr=over (impossible state but check)
def test_latched_true_prev_under_curr_over_still_alerts():
    """Defensive: if the latch is True but the prev→curr transition IS a
    rising edge (could happen if state was corrupted or bot restarted
    with stale latch), the function should still alert. This is the
    'rising edge' branch which is independent of the latch input."""
    alert, latched = _should_alert_over_cap(
        prev_exposure=40.0, curr_exposure=60.0, cap=CAP, latched=True
    )
    assert alert is True
    assert latched is True
