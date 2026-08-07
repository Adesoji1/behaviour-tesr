"""
Stage 8 — DECISION: map the ensemble risk score to OUR behavioural activity codes.

1.md: define our own codes from the actual behavioural signals — do not reuse 450-457. Each
decision has a status (safe/unsafe), an activity_code, and a human-readable description.

Tier cut-offs are percentiles of the training risk distribution (R3, until labels exist). The
specific code within the unsafe band is chosen from the strongest contributing signal, so the
reason is explainable.
"""
from __future__ import annotations

import numpy as np

from . import config

# code -> (status, short meaning)
CODES = {
    "BF-100": ("safe", "Consistent with the customer's behaviour"),
    "BF-110": ("safe", "Recurring / known pattern for this customer"),
    "BF-200": ("review", "Mild anomaly :monitor immediately"),
    "BF-301": ("unsafe", "Amount anomaly vs the customer's history"),
    "BF-302": ("unsafe", "Unusual time for this customer"),
    "BF-303": ("unsafe", "Unusual location for this customer"),
    "BF-304": ("unsafe", "New / unusual beneficiary or counterparty structure"),
    "BF-305": ("unsafe", "Velocity / burst anomaly"),
    "BF-400": ("unsafe", "Strong multi-signal anomaly:high risk"),
}

# Operational zones (fraud-team queues) — the SAME three tiers as the handover doc, derived from
# the decision status so they always agree with the dynamic thresholds. status -> (zone, label,
# recommended queue). BF codes are the "why"; the zone is the "which queue".
ZONES = {
    "safe":   ("clear_normal", "Clear Normal", "bypass manual review"),
    "review": ("review_grey", "Review / Grey Zone", "secondary manual review"),
    "unsafe": ("priority_1_unsafe", "Priority-1 Unsafe",
               "auto-block or priority analyst verification"),
}

# which feature drives which unsafe code (strongest wins)
_SIGNAL_TO_CODE = [
    ("amount", ("amt_z", "amt_over_max", "above_max", "amt_over_p95"), "BF-301"),
    ("time", ("hour_rarity", "dow_rarity", "is_night"), "BF-302"),
    ("location", ("location_new", "country_new", "cross_border", "ip_new"), "BF-303"),
    ("beneficiary", ("beneficiary_new", "g_shared_cp", "g_fanout", "g_distinct_benef"), "BF-304"),
    ("velocity", ("vel_1m", "vel_2m", "vel_3m", "vel_10m", "vel_15m", "vel_1h", "vel_24h",
                  "amt_1h_ratio"), "BF-305"),
]

# Human names for the ensemble detectors + the threshold above which a detector is reported as
# having "flagged" the transaction. Reported for transparency so an analyst sees WHICH model(s).
DETECTOR_LABELS = {"isoforest": "Isolation Forest", "autoencoder": "Autoencoder",
                   "gnn": "Graph-structure model"}
DETECTOR_HIGH = float(config.__dict__.get("DETECTOR_HIGH", 0.85))

# Behavioural signals: (machine code, analyst phrase, predicate on the feature vector).
# These populate triggered_signals / detection_reason DYNAMICALLY, so the response always
# explains WHY a transaction was flagged.
_FEATURE_SIGNALS = [
    ("amount_above_historical_max", "amount exceeds the customer's historical maximum",
     lambda f: f.get("above_max", 0) >= 1 or f.get("amt_over_max", 0) >= 1),
    ("amount_far_above_usual", "amount far above the customer's usual",
     lambda f: f.get("amt_z", 0) >= 3 or f.get("amt_over_median", 0) >= 5),
    ("new_beneficiary", "first-time beneficiary",
     lambda f: f.get("beneficiary_new", 0) >= 1),
    ("new_location", "location the customer has not used before",
     lambda f: f.get("location_new", 0) >= 1),
    ("new_country", "country the customer has not used before",
     lambda f: f.get("country_new", 0) >= 1),
    ("cross_border", "cross-border transfer",
     lambda f: f.get("cross_border", 0) >= 1),
    ("unusual_hour", "uncommon hour for this customer",
     lambda f: f.get("hour_rarity", 0) > 0.9),
    ("night_time", "night-time transaction",
     lambda f: f.get("is_night", 0) >= 1),
    ("unusual_day", "uncommon day of week for this customer",
     lambda f: f.get("dow_rarity", 0) > 0.9),
    ("velocity_burst", "burst of transactions in a short window (1-3 min)",
     # velocity counts are log1p-scaled: log1p(3)=1.39 (~3 txns/3min), log1p(2)=1.10 (~2 txns/1min)
     lambda f: f.get("vel_3m", 0) >= 1.39 or f.get("vel_1m", 0) >= 1.10),
    ("recent_activity", "recent transaction activity elevated the risk (below the burst threshold)",
     # sub-burst velocity: some recent activity moved the detectors but did not reach 'velocity_burst'.
     lambda f: (f.get("vel_3m", 0) < 1.39 and f.get("vel_1m", 0) < 1.10)
               and any(f.get(c, 0) > 0 for c in ("vel_1m", "vel_2m", "vel_3m", "vel_10m", "vel_15m", "vel_1h"))),
    ("shared_counterparty", "beneficiary is shared by many customers (possible collector)",
     lambda f: f.get("g_shared_cp", 0) >= 2),
    ("rare_transaction_type", "an unusual transaction type for this customer",
     lambda f: f.get("type_rare", 0) >= 1),                       # binary novelty flag
    ("new_ip", "a device / IP address the customer has not used before",
     lambda f: f.get("ip_new", 0) >= 1),                          # binary novelty flag
    ("high_fanout", "funds sent to an unusually large number of distinct recipients (fan-out)",
     # g_fanout = distinct/total (0-1). Require it near-total AND enough distinct beneficiaries
     # (g_distinct_benef = log1p(n); log1p(4)=1.61) so a 1-2 txn customer cannot trip it.
     lambda f: f.get("g_fanout", 0) >= 0.9 and f.get("g_distinct_benef", 0) >= 1.6),
    ("hourly_amount_spike", "amount far above the customer's recent hourly average",
     # amt_1h_ratio = log1p(1h spend / median); log1p(6.4)=2.0 -> ~6x the usual in one hour.
     lambda f: f.get("amt_1h_ratio", 0) >= 2.0),
]


# For a cold-start customer there is no learned personal history, so the amount-vs-"usual" phrases
# would be misleading. Swap them for population-baseline wording (same signal, honest framing).
_COLD_START_REASON = {
    "amount_far_above_usual": "transaction amount is far above the population baseline",
    "amount_above_historical_max": "amount exceeds the population baseline maximum",
}


def explain(feats: dict | None, detector_scores: dict | None = None,
            is_cold_start: bool = False) -> tuple[list[str], list[str]]:
    """Return (triggered_signals, detection_reason): machine codes + analyst-readable reasons.
    Combines behavioural-feature signals with the ensemble-detector evidence, so the response is
    never empty when a transaction is flagged — even for a cold-start customer."""
    feats = feats or {}
    detector_scores = detector_scores or {}
    signals, reasons = [], []
    for code, phrase, pred in _FEATURE_SIGNALS:
        try:
            if pred(feats):
                signals.append(code)
                reasons.append(_COLD_START_REASON[code] if is_cold_start and code in _COLD_START_REASON
                               else phrase)
        except Exception:
            pass
    # which detector(s) actually flagged it, strongest first — always available evidence
    det_high = sorted(((k, float(v)) for k, v in detector_scores.items() if float(v) >= DETECTOR_HIGH),
                      key=lambda x: -x[1])
    for k, s in det_high:
        signals.append(f"detector:{k}")
        reasons.append(f"{DETECTOR_LABELS.get(k, k)} anomaly score {s:.2f}")
    if is_cold_start:
        signals.append("cold_start")
        reasons.append("no learned profile for this customer yet (cold-start) — judged against "
                       "the population baseline")
    return signals, reasons


class Tiering:
    """Fits percentile cut-offs on training risk scores, then labels new scores."""

    def __init__(self, percentiles: dict | None = None):
        self.p = {**config.TIER_PERCENTILES, **(percentiles or {})}
        self.cuts: dict = {}

    def fit(self, train_risk: np.ndarray) -> "Tiering":
        r = np.asarray(train_risk, dtype=float)
        self.cuts = {
            "unsafe_high": float(np.percentile(r, self.p["unsafe_high"])),
            "unsafe": float(np.percentile(r, self.p["unsafe"])),
            "review": float(np.percentile(r, self.p["review"])),
        }
        return self

    def decide(self, risk: float, feats: dict | None = None,
               detector_scores: dict | None = None, is_cold_start: bool = False) -> dict:
        """Return {status, activity_code, description} for one risk score. The activity_code
        names the PRIMARY behavioural finding; the description explains the EVIDENCE — which
        detector(s) fired and the behavioural deviations — so a fraud analyst sees what happened."""
        feats = feats or {}
        if risk >= self.cuts.get("unsafe_high", 0.995):
            code = "BF-400"
        elif risk >= self.cuts.get("unsafe", 0.98):
            code = self._pick_unsafe(feats)
        elif risk >= self.cuts.get("review", 0.90):
            code = "BF-200"
        else:
            code = "BF-110" if risk < 0.5 else "BF-100"
        status, meaning = CODES[code]
        zone, zone_label, zone_action = ZONES[status]
        return {"status": status, "activity_code": code,
                "zone": zone, "zone_label": zone_label, "recommended_queue": zone_action,
                "description": self._describe(meaning, status, feats, detector_scores, is_cold_start)}

    def _pick_unsafe(self, feats: dict) -> str:
        best, best_code = -1.0, "BF-301"
        for _name, cols, code in _SIGNAL_TO_CODE:
            val = max((abs(float(feats.get(c, 0.0))) for c in cols), default=0.0)
            if val > best:
                best, best_code = val, code
        return best_code

    def _describe(self, meaning, status, feats, detector_scores=None, is_cold_start=False) -> str:
        """Decision-explaining description, generated dynamically from the evidence (no hard-coded
        sentence). It states the DECISION first, then WHY, then what the models saw."""
        feats = feats or {}
        detector_scores = detector_scores or {}
        # cold-start has no learned baseline → the amount is judged vs the POPULATION ("the typical
        # amount"), not the customer's own ("the customer's typical amount").
        usual = "the typical amount" if is_cold_start else "the customer's typical amount"

        # --- AMOUNT evidence (leads the narrative as its own sentence) --------------------------------
        amt_noun = ""      # lowercase fragment, for the SAFE "some variation was noted (…)" list
        amt_sentence = ""  # capitalised standalone sentence, for the review/unsafe narrative
        amt_x = feats.get("amt_over_median", 0)
        above_max = (feats.get("above_max", 0) >= 1 or feats.get("amt_over_max", 0) >= 1) and not is_cold_start
        if amt_x >= 2:
            tail = " and exceeds their historical maximum" if above_max else ""
            amt_noun = f"the transaction amount is approximately {int(round(amt_x)):,}× {usual}{tail}"
        elif above_max:
            amt_noun = "the transaction amount exceeds the customer's historical maximum"
        if amt_noun:
            amt_sentence = amt_noun[0].upper() + amt_noun[1:] + "."

        # --- OTHER behavioural signals as (noun phrase for the SAFE list, verb phrase for the ------
        # review/unsafe narrative). Both are generated dynamically from the SAME features. ---------
        ev = []
        if feats.get("beneficiary_new", 0) >= 1:
            ev.append(("a new beneficiary", "involves a first-time beneficiary"))
        if feats.get("location_new", 0) >= 1 or feats.get("country_new", 0) >= 1:
            ev.append(("a new location", "originates from a new location"))
        if feats.get("cross_border", 0) >= 1:
            ev.append(("cross-border activity", "crosses international borders"))
        if feats.get("hour_rarity", 0) > 0.9:
            night = feats.get("is_night", 0) >= 1
            ev.append(("an unusual hour" + (", at night" if night else ""),
                       "occurs at an unusual hour" + (" (night-time)" if night else "")))
        elif feats.get("is_night", 0) >= 1:
            ev.append(("a night-time transaction", "occurs at night"))
        if feats.get("vel_3m", 0) >= 1.39 or feats.get("vel_1m", 0) >= 1.10:
            n = max(int(round(np.expm1(max(feats.get("vel_3m", 0), feats.get("vel_1m", 0))))), 2)
            ev.append((f"a burst of approximately {n} transactions within a few minutes",
                       f"follows a burst of approximately {n} transactions within minutes"))
        elif any(feats.get(c, 0) > 0 for c in ("vel_1m", "vel_2m", "vel_3m", "vel_10m", "vel_15m", "vel_1h")):
            ev.append(("recent transaction activity that raised the risk (below the burst threshold)",
                       "follows recent account activity that raised the risk (below the burst threshold)"))
        if feats.get("amt_1h_ratio", 0) >= 2.0:
            ev.append(("a spike in their spending over the last hour", "spikes the last hour's spending"))
        if feats.get("g_fanout", 0) >= 0.9 and feats.get("g_distinct_benef", 0) >= 1.6:
            ev.append(("funds fanned out to many distinct recipients",
                       "fans funds out to many distinct recipients"))
        if feats.get("type_rare", 0) >= 1:
            ev.append(("an unusual transaction type", "uses an unusual transaction type"))
        if feats.get("ip_new", 0) >= 1:
            ev.append(("a new device/IP", "uses a previously unseen device/IP"))
        # --- detectors that scored high (the model evidence) ---
        dets = [DETECTOR_LABELS[k] for k, v in detector_scores.items()
                if k in DETECTOR_LABELS and float(v) >= DETECTOR_HIGH]

        def _join(items, sep="; ", last="; and "):
            items = list(items)
            return items[0] if len(items) <= 1 else sep.join(items[:-1]) + last + items[-1]

        # --- SAFE: never contradict. Acknowledge minor variation but frame it as below threshold ---
        if status == "safe":
            nouns = ([amt_noun] if amt_noun else []) + [n for n, _ in ev]
            if nouns:
                return ("Safe. The transaction is generally consistent with the customer's "
                        "behavioural profile. Some variation was noted (" + _join(nouns) + "), but "
                        "the overall behavioural risk remains below the review threshold.")
            return "Safe. Consistent with the customer's behaviour."

        # --- REVIEW / UNSAFE: an investigation-summary narrative that an analyst can scan -----------
        strength = (1 if amt_noun else 0) + len(ev) + len(dets)
        if status == "unsafe":
            lead = "Strong multi-signal behavioural anomaly — high risk; immediate review recommended."
        else:  # review
            lead = ("Strong behavioural anomaly detected — review recommended."
                    if strength >= 3 else "Mild behavioural anomaly — monitor.")
        s = lead
        if amt_sentence:                         # primary evidence, leads as its own sentence
            s += " " + amt_sentence
        if ev:                                   # "In addition, the transaction involves …, uses …"
            verbs = [v for _, v in ev]
            connector = "In addition, the transaction " if amt_sentence else "The transaction "
            s += " " + connector + _join(verbs, sep=", ", last=", and ") + "."
        if is_cold_start:                        # evaluation CONTEXT, kept separate from the evidence
            s += (" The customer is in cold-start, so the transaction was evaluated against the "
                  "population baseline rather than an established personal behavioural profile.")
        if dets:
            joined = dets[0] if len(dets) == 1 else " and ".join([", ".join(dets[:-1]), dets[-1]])
            also = "also " if (amt_sentence or ev) else ""
            s += f" {joined} {also}produced very high anomaly scores."
        return s

    def to_dict(self) -> dict:
        return {"percentiles": self.p, "cuts": self.cuts}

    @classmethod
    def from_dict(cls, d: dict) -> "Tiering":
        obj = cls(d.get("percentiles"))
        obj.cuts = d.get("cuts", {})
        return obj
