"""
Model performance over time + health gate (PDF §11/§12 workflow step 12).

`record()` appends every trained model's headline metrics to a history file so performance can be
tracked run-over-run. `check_health()` compares the model against the acceptance floors and, if it
falls below them, flags it UNHEALTHY and raises an alert (to Slack when BF_SLACK_WEBHOOK_URL is
set) warning that continued use is unsafe — i.e. the model should be retrained or rolled back.

Until confirmed-fraud labels exist there is no analyst-verified precision, so the unsupervised
synthetic-anomaly AUC is the PRIMARY health signal; the proxy-precision floor is wired and
configurable for when real labels arrive (config.MIN_ACCEPTABLE_*).

    python -m ml.monitor            # show the active model's (training-time) health
    python -m ml.monitor --live     # watch the model in PRODUCTION (reads bp_decision) + alert

MLOps division of labour: the SERVICE only serves /score and records decisions to bp_decision;
this module (ML side) watches those records, alerts to Slack with the exact retraining steps, and
`ml.retrain_trigger --run` does the retrain + acceptance-gated promote. The service then reloads
the newly-promoted model from the registry (POST /reload). No training logic lives in the service.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone

from . import config, registry

log = logging.getLogger("ml.monitor")
HISTORY = config.DIR_MONITOR / "performance_history.jsonl"
HEALTH = config.DIR_MONITOR / "health.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(version: str, manifest: dict) -> dict:
    """Append this model's headline metrics to the performance-over-time history (JSONL)."""
    met = manifest.get("metrics", {}) or {}
    uns = manifest.get("unsupervised_validation") or {}
    row = {
        "logged_at": _now(),
        "version": version,
        "synthetic_auc": uns.get("synthetic_auc"),
        "contamination_gap": uns.get("contamination_gap"),
        "proxy_precision": met.get("precision"),
        "proxy_recall": met.get("recall"),
        "proxy_f1": met.get("f1"),
        "proxy_roc_auc": met.get("roc_auc"),
        "eligible_customers": (manifest.get("data") or {}).get("eligible_customers"),
    }
    with open(HISTORY, "a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    log.info("monitor: recorded performance for %s", version)
    return row


def write_thresholds(version: str | None = None) -> dict:
    """Regenerate the fraud-team threshold handover from the model's OWN dynamic tiering cuts, so
    what to tell the fraud team is ALWAYS current and never hand-maintained. Writes both a machine
    file (thresholds.json) and a human handover (THRESHOLDS.md). Called at every retrain; can also
    be run against the active model at any time. Boundaries are the model's calibrated quantiles —
    nothing hard-coded here."""
    from . import codes
    v = version or registry.active()
    if not v:
        return {}
    cuts = json.loads((registry.model_dir(v) / "tiering.json").read_text())
    c, p = cuts.get("cuts", {}), cuts.get("percentiles", {})
    review, unsafe = c.get("review"), c.get("unsafe")
    zones = {
        codes.ZONES["safe"][0]: {"status": "safe", "range": f"score < {review:.4f}",
                                 "upper": review, "action": codes.ZONES["safe"][2]},
        codes.ZONES["review"][0]: {"status": "review", "range": f"{review:.4f} <= score < {unsafe:.4f}",
                                   "lower": review, "upper": unsafe, "action": codes.ZONES["review"][2]},
        codes.ZONES["unsafe"][0]: {"status": "unsafe", "range": f"score >= {unsafe:.4f}",
                                   "lower": unsafe, "action": codes.ZONES["unsafe"][2]},
    }
    doc = {"model": v, "generated_at": _now(), "percentile_levels": p, "cuts": c, "zones": zones,
           "note": "Dynamic quantile boundaries, recomputed on every retrain from the held-out "
                   "distribution. Do NOT hard-code — read them from here or GET /thresholds."}
    (config.DIR_MONITOR / "thresholds.json").write_text(json.dumps(doc, indent=2))
    md = (
        f"# Operational Handover — Calibrated Behavioural Risk Thresholds\n\n"
        f"**Model:** `{v}` · **Generated:** {doc['generated_at']}\n"
        f"**Calibration:** dynamic, quantile-based zones (p{p.get('review')}/p{p.get('unsafe')}) "
        f"recomputed each retrain — not static scores.\n\n"
        f"| Zone | Score range | Status | Fraud-team action |\n|---|---|---|---|\n"
        f"| 🟥 Priority-1 Unsafe | score ≥ {unsafe:.4f} | unsafe | auto-block or priority analyst verification |\n"
        f"| 🟨 Review / Grey Zone | {review:.4f} ≤ score < {unsafe:.4f} | review | secondary manual review |\n"
        f"| 🟩 Clear Normal | score < {review:.4f} | safe | bypass manual review |\n\n"
        f"> Higher score = greater deviation from the learned baseline. These are the model's own\n"
        f"> calibrated quantile boundaries; the `/score` response also returns the `zone` directly.\n"
    )
    (config.DIR_MONITOR / "THRESHOLDS.md").write_text(md)
    log.info("monitor: wrote thresholds handover for %s (review=%.4f unsafe=%.4f)", v, review, unsafe)
    return doc


def check_health(version: str, manifest: dict, alert: bool = True) -> dict:
    """Compare against the acceptance floors; alert (Slack) if the model is unhealthy."""
    met = manifest.get("metrics", {}) or {}
    uns = manifest.get("unsupervised_validation") or {}
    sauc, prec = uns.get("synthetic_auc"), met.get("precision")
    problems = []
    if sauc is not None and sauc < config.MIN_ACCEPTABLE_SYNTH_AUC:
        problems.append(f"synthetic-anomaly AUC {sauc:.3f} < floor {config.MIN_ACCEPTABLE_SYNTH_AUC}")
    if prec is not None and prec < config.MIN_ACCEPTABLE_PRECISION:
        problems.append(f"precision {prec:.3f} < floor {config.MIN_ACCEPTABLE_PRECISION}")
    status = {
        "checked_at": _now(), "version": version, "healthy": not problems, "problems": problems,
        "synthetic_auc": sauc, "precision": prec,
        "floors": {"synthetic_auc": config.MIN_ACCEPTABLE_SYNTH_AUC,
                   "precision": config.MIN_ACCEPTABLE_PRECISION},
    }
    HEALTH.write_text(json.dumps(status, indent=2))
    if problems and alert:
        _alert(version, problems)
    elif not problems:
        log.info("monitor: %s healthy (synthetic AUC=%s, precision=%s)", version, sauc, prec)
    return status


# The exact production steps an on-call engineer should take when an alert fires. Kept in the
# alert itself so the Slack message is self-contained.
_RETRAIN_STEPS = (
    "*What to do:*\n"
    "1. On the training host (GPU): `docker compose --profile train run --rm trainer --promote` "
    "(or `python -m ml.retrain_trigger --run`).\n"
    "2. It retrains and PROMOTES the new model *only if it beats the active one* "
    "(synthetic-anomaly AUC / precision gate) — otherwise it stays on the current model and says so.\n"
    "3. If promoted, roll it out to serving: `curl -X POST http://<service>:8080/reload` "
    "(or restart the behaviour-profile service) to load the new active model — no downtime.\n"
    "4. To revert: `python -c \"from ml import registry; registry.rollback()\"` then reload again."
)


def live_precision() -> dict | None:
    """REAL precision/recall from the analyst feedback loop (bp_decision_feedback, POST /feedback),
    joining each transaction's LATEST decision to the analyst's confirmed verdict over the recent
    feedback window. A flagged (review+unsafe) decision the analyst confirmed 'fraud' is a true
    positive; confirmed 'genuine' is a false positive; a 'safe' decision later confirmed 'fraud' is
    a missed fraud (false negative). Returns None until at least config.LIVE_MIN_LABELS verdicts
    exist (before that the number is noise). This is the metric that replaces the flag-rate proxy
    once labels arrive (PDF §11)."""
    from . import db
    days = config.LIVE_PRECISION_WINDOW_DAYS
    try:
        rows = db.read_sql(
            "WITH latest AS ("
            "  SELECT DISTINCT ON (transaction_id) transaction_id, decision "
            f"  FROM {config.DECISION_TABLE} WHERE transaction_id IS NOT NULL "
            "  ORDER BY transaction_id, scored_at DESC) "
            "SELECT l.decision AS decision, lower(f.verdict) AS verdict, count(*) AS n "
            f"FROM {config.FEEDBACK_TABLE} f JOIN latest l ON l.transaction_id = f.transaction_id "
            "WHERE f.created_at > now() - make_interval(days => %s) "
            "GROUP BY l.decision, lower(f.verdict)", (days,))
    except Exception as e:                       # no feedback table yet / store down
        log.info("monitor(live): no precision (feedback unavailable: %s)", e)
        return None
    if rows is None or rows.empty:
        return None
    tp = fp = fn = tn = 0
    for _, r in rows.iterrows():
        flagged = str(r["decision"]) in ("review", "unsafe")
        fraud = str(r["verdict"]) == "fraud"
        n = int(r["n"])
        if flagged and fraud:     tp += n
        elif flagged and not fraud: fp += n
        elif not flagged and fraud: fn += n
        else:                       tn += n
    labels = tp + fp + fn + tn
    if labels < config.LIVE_MIN_LABELS:
        return {"labels": labels, "enough": False, "window_days": days,
                "min_labels": config.LIVE_MIN_LABELS}
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return {"labels": labels, "enough": True, "window_days": days,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None}


def check_live(alert: bool = True) -> dict:
    """Watch the model in PRODUCTION: read the decisions the service wrote to bp_decision over the
    recent window and flag drift. Signal today = the flagged (review+unsafe) rate vs its expected
    band; REAL precision/recall is folded in here once analyst feedback exists (live_precision)."""
    from . import db
    win, tbl = config.LIVE_WINDOW_HOURS, config.DECISION_TABLE
    try:
        rows = db.read_sql(
            f"SELECT decision, count(*) AS n FROM {tbl} "
            f"WHERE scored_at > now() - make_interval(hours => %s) GROUP BY decision", (win,))
    except Exception as e:
        log.warning("monitor(live): could not read %s (%s)", tbl, e)
        return {"checked_at": _now(), "error": str(e)}
    counts = {r["decision"]: int(r["n"]) for _, r in rows.iterrows()} if not rows.empty else {}
    total = sum(counts.values())
    flagged = sum(v for k, v in counts.items() if k in ("review", "unsafe"))
    flag_rate = (flagged / total) if total else 0.0
    problems = []
    if total < config.LIVE_MIN_SAMPLE:
        note = f"only {total} decisions in {win}h (< {config.LIVE_MIN_SAMPLE}) — not enough to judge"
    else:
        note = f"{total} decisions in {win}h; flagged (review+unsafe) rate {flag_rate:.1%}"
        if flag_rate > config.LIVE_FLAG_RATE_MAX:
            problems.append(f"flagged rate {flag_rate:.1%} > {config.LIVE_FLAG_RATE_MAX:.0%} — "
                            "over-flagging (data/behaviour drift or miscalibration)")
        if flag_rate < config.LIVE_FLAG_RATE_MIN:
            problems.append(f"flagged rate {flag_rate:.1%} < {config.LIVE_FLAG_RATE_MIN:.0%} — "
                            "under-flagging (the model may be stale)")
    # REAL precision/recall from the analyst feedback loop (once enough verdicts exist, §11).
    prec = live_precision()
    if prec and prec.get("enough"):
        p = prec.get("precision")
        note += (f"; analyst-labelled precision {p:.1%} "
                 f"(TP={prec['tp']} FP={prec['fp']} FN={prec['fn']}, {prec['labels']} verdicts/"
                 f"{prec['window_days']}d)") if p is not None else ""
        if p is not None and p < config.MIN_ACCEPTABLE_PRECISION:
            problems.append(f"analyst-confirmed precision {p:.1%} < floor "
                            f"{config.MIN_ACCEPTABLE_PRECISION:.0%} — too many false positives")
    elif prec:
        note += (f"; {prec['labels']}/{prec['min_labels']} analyst verdicts so far "
                 "(need more before precision is meaningful)")

    status = {"checked_at": _now(), "active_model": registry.active(), "window_hours": win,
              "total_decisions": total, "flag_rate": round(flag_rate, 4), "by_decision": counts,
              "healthy": not problems, "problems": problems, "note": note,
              "precision": prec,
              "band": [config.LIVE_FLAG_RATE_MIN, config.LIVE_FLAG_RATE_MAX]}
    (config.DIR_MONITOR / "live_health.json").write_text(json.dumps(status, indent=2))
    if problems and alert:
        _alert(registry.active() or "active", problems, live=True)
    else:
        log.info("monitor(live): %s", note)
    return status


def _alert(version: str, problems: list[str], live: bool = False) -> None:
    scope = "in PRODUCTION" if live else "at training"
    msg = (f":rotating_light: Behavioural anti-fraud model *{version}* looks UNHEALTHY {scope}.\n"
           + "\n".join(f"• {p}" for p in problems) + "\n" + _RETRAIN_STEPS)
    log.warning("MODEL HEALTH ALERT (%s) %s | %s", scope, version, "; ".join(problems))
    if config.SLACK_WEBHOOK_URL:
        try:
            import httpx
            httpx.post(config.SLACK_WEBHOOK_URL, json={"text": msg}, timeout=5.0)
            log.info("monitor: posted health alert to Slack")
        except Exception as e:
            log.warning("monitor: Slack alert failed: %s", e)
    else:
        log.warning("monitor: Slack not configured (BF_SLACK_WEBHOOK_URL) — alert logged only")


def main():
    ap = argparse.ArgumentParser(description="Behavioural model monitor")
    ap.add_argument("--live", action="store_true", help="watch the model in production (bp_decision)")
    a = ap.parse_args()
    if a.live:
        print(json.dumps(check_live(alert=True), indent=2, default=str))
        return
    v = registry.active()
    if not v:
        print(json.dumps({"error": "no active model"}))
        return
    print(json.dumps(check_health(v, registry.get(v) or {}, alert=False), indent=2, default=str))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    config.attach_file_log()
    main()
