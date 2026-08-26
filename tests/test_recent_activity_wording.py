"""Regression tests: sub-burst 'recent_activity' wording + the burst-threshold boundary.

Context / why this exists
-------------------------
The velocity features (vel_1m..vel_24h) are part of the FEATURES vector fed to the
Isolation Forest + Autoencoder, so ANY non-zero velocity moves the detectors — even when
it does not cross the 'velocity_burst' cutoff (vel_3m >= 1.39 OR vel_1m >= 1.10). The old
phrase "recent transaction activity ... raised the risk (below the burst threshold)" read
as if the *risk* were below a threshold; it was reworded to say the activity contributed to
the risk although the *burst* threshold was not reached.

This is a TEXT-ONLY change. These tests pin down that the SIGNAL LOGIC, THRESHOLDS and the
machine-readable RESPONSE CONTRACT (signal codes) are UNCHANGED, and that the corrected
wording is present while the old ambiguous phrasing is gone. Scoring, thresholds, the
feature vector and the detectors are untouched.

Run standalone:  .venv-ml/bin/python tests/test_recent_activity_wording.py
Or with pytest:  pytest tests/test_recent_activity_wording.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root on path

from ml import codes

# The documented burst cutoffs (must not drift). log1p(~3 txns/3min)=1.39, log1p(~2/1min)=1.10.
BURST_VEL_3M = 1.39
BURST_VEL_1M = 1.10
CORRECTED = "contributed to the elevated risk score, although the burst threshold was not reached"


def _explain(feats, cold=False):
    return codes.explain(feats, {}, is_cold_start=cold)


# ---- signal logic + thresholds (unchanged) ---------------------------------------------------
def test_recent_activity_fires_below_burst_threshold():
    signals, _ = _explain({"vel_10m": 0.7, "vel_1m": 0.0, "vel_3m": 0.0})
    assert "recent_activity" in signals
    assert "velocity_burst" not in signals


def test_velocity_burst_fires_at_vel3m_cutoff():
    signals, _ = _explain({"vel_3m": BURST_VEL_3M})
    assert "velocity_burst" in signals
    assert "recent_activity" not in signals   # the two are mutually exclusive by construction


def test_velocity_burst_fires_at_vel1m_cutoff():
    signals, _ = _explain({"vel_1m": BURST_VEL_1M})
    assert "velocity_burst" in signals
    assert "recent_activity" not in signals


def test_no_velocity_means_neither_signal():
    signals, _ = _explain({c: 0 for c in ("vel_1m", "vel_2m", "vel_3m", "vel_10m", "vel_15m", "vel_1h")})
    assert "recent_activity" not in signals
    assert "velocity_burst" not in signals


def test_burst_boundary_preserved():
    # just under both cutoffs -> sub-burst 'recent_activity', never 'velocity_burst'
    below = {"vel_3m": BURST_VEL_3M - 0.01, "vel_1m": BURST_VEL_1M - 0.01}
    s_below, _ = _explain(below)
    assert "recent_activity" in s_below and "velocity_burst" not in s_below
    # exactly at the cutoff -> 'velocity_burst'
    s_at, _ = _explain({"vel_3m": BURST_VEL_3M})
    assert "velocity_burst" in s_at


# ---- response-contract signal codes (must not rename) ----------------------------------------
def test_signal_codes_contract_unchanged():
    known = {c for c, _, _ in codes._FEATURE_SIGNALS}
    assert "recent_activity" in known
    assert "velocity_burst" in known


# ---- wording (corrected in the reason + the review/unsafe narrative; SAFE stays non-contradictory)
def test_detection_reason_wording_corrected():
    _, reasons = _explain({"vel_10m": 0.7})
    joined = " ".join(reasons)
    assert CORRECTED in joined
    assert "below the burst threshold" not in joined      # old ambiguous parenthetical gone from the reason


def test_unsafe_description_wording_corrected():
    d = codes.Tiering().decide(0.99, {"vel_10m": 0.7}, {}, is_cold_start=True)  # 0.99 >= default unsafe cut
    assert d["status"] == "unsafe"
    assert CORRECTED in d["description"]
    assert "raised the risk (below the burst threshold)" not in d["description"]


def test_safe_description_not_contradictory():
    d = codes.Tiering().decide(0.0, {"vel_10m": 0.7})   # low risk -> safe
    assert d["status"] == "safe"
    assert "elevated risk" not in d["description"]        # a SAFE verdict must never claim elevated risk
    assert "recent transaction activity below the burst threshold" in d["description"]


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
