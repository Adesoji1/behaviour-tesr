"""
Profile trust / eligibility gate — Practical rules §1/§2/§10.

This is NOT AML. It answers one behavioural question: does a customer have a rich enough,
clean enough, confident enough profile that we can judge them on their OWN learned history?
Re-checked at decision time so a policy change (or newly-seen fraud) takes effect immediately.

Extracted from the retired rule engine so the AML/rule code can be removed entirely while this
behavioural-eligibility helper (used by the profile inspection endpoints) lives on.
"""
from __future__ import annotations

import config


def profile_is_trusted(p: dict | None) -> tuple[bool, str]:
    """Return (trusted, reason). Trusted = the customer clears §1/§2/§10 and can be judged on
    their own profile; otherwise they are still warming up (judged against peers)."""
    if p is None:
        return False, "peer_baseline (new account, no own history)"
    if p.get("profile_status") != "active":
        return False, f"peer_baseline ({p.get('profile_status')})"
    if (p.get("confidence_score") or 0) < config.CONFIDENCE_TRUST_THRESHOLD:
        return False, f"peer_baseline (low confidence {p.get('confidence_score')})"
    # §1 "No confirmed fraud cases"
    if (p.get("suspicious_tx_count") or 0) > config.ELIGIBLE_MAX_FRAUD_TXNS:
        return False, (f"peer_baseline (§1 confirmed fraud: "
                       f"{p.get('suspicious_tx_count')} txn(s))")
    # §1/§2 minimum-data gate, re-checked live against current policy
    if (p.get("tenure_days") or 0) < config.ELIGIBLE_MIN_TENURE_DAYS:
        return False, (f"peer_baseline (§1 tenure {p.get('tenure_days')}d "
                       f"< {config.ELIGIBLE_MIN_TENURE_DAYS}d)")
    if (p.get("lifetime_clean_txns") or 0) < config.ELIGIBLE_MIN_TXNS:
        return False, (f"peer_baseline (§1 clean txns {p.get('lifetime_clean_txns')} "
                       f"< {config.ELIGIBLE_MIN_TXNS})")
    return True, "own_profile"
