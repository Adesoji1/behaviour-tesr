#!/usr/bin/env python3
"""
Step 3 — Load reference data the rule engine needs:
  (a) the AML rule catalogue (from AML_Rules.xlsx) into bp_rule_definition, and
  (b) a read-only mirror of production users_blacklist into bp_blacklist.

Each rule gets a stable rule_code and a `params` JSON of default thresholds /
window sizes. These are the knobs the rule engine reads at evaluation time; the
behaviour profile supplies the per-customer baseline the rule compares against.
"""
import json
import os
import re
import subprocess

import openpyxl

import config
import db

# Default thresholds per rule. Windows in seconds unless noted. Amounts in NGN.
RULE_PARAMS = {
    "flag_blacklisted_users_transactions": {"source": "profile.is_blacklisted OR bp_blacklist"},
    "detect_unusual_country": {"baseline": "usual_countries", "min_history": 5},
    "detect_unusual_city": {"baseline": "usual_cities", "min_history": 5},
    "unusual_time_for_user": {"baseline": "peak_transaction_hours", "min_share": 0.02, "min_history": 10},
    "excessive_beneficiary_count_24h": {"window": "24h", "max_distinct_beneficiaries": 20},
    "high_recipient_count_1h": {"window": "1h", "max_distinct_recipients": 10},
    "high_outbound_amount_15m": {"window": "15m", "max_amount": 5000000},
    "many_users_to_one_account_1h": {"window": "1h", "max_distinct_senders": 10},
    "transaction_velocity_1m": {"window": "1m", "max_count": 3},
    "escalate_single_transfer_above_10m_ngn": {"max_amount": 10000000, "currency": "NGN"},
    "escalate_foreign_above_10k_usd": {"max_amount": 10000, "currency": "USD"},
    "outbound_exceeds_historical_max": {"baseline": "max_amount", "factor": 1.0},
    "type_exceeds_daily_amount_threshold": {"window": "24h", "per_type": True},
    "weekly_outgoing_high_expenditure": {"window": "7d", "factor_over_avg": 3.0},
    "blacklisted_user_detection": {"source": "bp_blacklist"},
    "blacklisted_beneficiary_detection": {"source": "bp_blacklist", "match": "destination_account_no"},
    "first_time_beneficiary": {"baseline": "beneficiaries"},
    "suspicious_beneficiary_detection": {"source": "bp_blacklist"},
    "cross_border_velocity": {"window": "24h", "max_countries": 2},
    "structured_inflow_detection": {"window": "24h", "near_threshold": 1000000, "band": 0.1, "min_count": 3},
    "dormant_account_reactivation": {"dormancy_days": 90},
    "high_value_after_dormancy": {"dormancy_days": 90, "factor_over_avg": 3.0},
    "block_repeated_low_amount": {"window": "1h", "amount_lte": 100, "min_count": 5},
    "block_significantly_high_amount": {"baseline": "p95_amount", "factor": 3.0},
    "flag_high_frequency_1d": {"window": "24h", "max_count": 50},
    "block_above_hard_cap": {"hard_cap": 1000000000},
    "escalate_structuring_7d": {"window": "7d", "near_threshold": 1000000, "band": 0.1, "min_count": 5},
    "excessive_beneficiary_count": {"window": "30d", "max_distinct_beneficiaries": 100},
    "flag_high_frequency_10m": {"window": "10m", "max_count": 10},
    "multiple_countries_short_timeframe": {"window": "1h", "max_countries": 2},
    "rapid_transactions_after_reactivation": {"dormancy_days": 90, "window": "1h", "max_count": 5},
    "flag_cross_border_transfer": {"condition": "origin_country != destination_country"},
}


# Canonical rule_code for each AML_Rules.xlsx rule NAME. This keeps the codes
# stored in bp_rule_definition identical to the codes the rule engine references
# (a raw slug of the name would drift, e.g. "Block Transaction Above Hard Cap").
NAME_TO_CODE = {
    "Flag Blacklisted Users Transactions": "flag_blacklisted_users_transactions",
    "Detects transactions from an unusual country": "detect_unusual_country",
    "Detects any transaction from an unusual city": "detect_unusual_city",
    "Flag transaction that occur at an unusual time for a user": "unusual_time_for_user",
    "Flags excessive beneficiary count in 24 hours": "excessive_beneficiary_count_24h",
    "High count of transactions to different recipients in 1 hour": "high_recipient_count_1h",
    "High outbound amount in 15 minutes": "high_outbound_amount_15m",
    "Multiple transactions from different users to an account within 1 hour": "many_users_to_one_account_1h",
    "Flag transaction velocity within 1 minutes": "transaction_velocity_1m",
    "Escalate single transfer transactions above 10,000,000 NGN": "escalate_single_transfer_above_10m_ngn",
    "Escalates foreign transactions above $10,000": "escalate_foreign_above_10k_usd",
    "Detects Outbound Transactions Exceeds Historical Maximum": "outbound_exceeds_historical_max",
    "Flags Transaction Type that exceeds Daily Amount Threshold": "type_exceeds_daily_amount_threshold",
    "Monitor User Weekly Outgoing High Expenditure": "weekly_outgoing_high_expenditure",
    "Blacklisted User Detection": "blacklisted_user_detection",
    "Blacklisted Beneficiary Detection": "blacklisted_beneficiary_detection",
    "First-Time Beneficiary Transaction": "first_time_beneficiary",
    "Suspicious Beneficiary Detection": "suspicious_beneficiary_detection",
    "Cross-Border Velocity": "cross_border_velocity",
    "Structured Inflow Detection": "structured_inflow_detection",
    "Dormant Account Reactivation Detection": "dormant_account_reactivation",
    "High-Value Transaction After Dormancy": "high_value_after_dormancy",
    "Block repeated low transaction amount": "block_repeated_low_amount",
    "Blocks significantly high transaction amount": "block_significantly_high_amount",
    "Flag High Frequency - 1 Day": "flag_high_frequency_1d",
    "Block Transaction Above Hard Cap": "block_above_hard_cap",
    "Escalate Structuring Pattern - 7 Days": "escalate_structuring_7d",
    "Excessive Beneficiary Count Detection": "excessive_beneficiary_count",
    "Flag high transaction frequency within 10 minutes": "flag_high_frequency_10m",
    "Flag transactions from multiple countries within a short timeframe": "multiple_countries_short_timeframe",
    "Rapid Transactions After Reactivation": "rapid_transactions_after_reactivation",
    "Flag Cross-Border Transfer": "flag_cross_border_transfer",
}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:64]


def load_rules(cur):
    wb = openpyxl.load_workbook(os.path.join(config.BASE_DIR, "AML_Rules.xlsx"), data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))[1:]  # skip header
    n = 0
    for category, rule in rows:
        if not rule:
            continue
        name = str(rule).strip()
        code = NAME_TO_CODE.get(name) or slug(name)
        params = RULE_PARAMS.get(code, {})
        cur.execute(
            "INSERT INTO bp_rule_definition (rule_code, category, name, description, params, enabled) "
            "VALUES (%s,%s,%s,%s,%s,1) ON CONFLICT (rule_code) DO UPDATE SET "
            "category=EXCLUDED.category, name=EXCLUDED.name, params=EXCLUDED.params",
            (code, str(category), str(rule), str(rule), json.dumps(params)),
        )
        n += 1
    print(f"[rules] loaded {n} AML rules into bp_rule_definition")


def mirror_blacklist(cur):
    """Read-only pull of users_blacklist from production via psql, insert to bp_blacklist."""
    env = dict(os.environ)
    env["PGPASSWORD"] = config.PROD_PG["password"]
    q = ("SELECT id, blacklist_type, source, risk_level, name, entity_type, "
         "identifier_type, identifier, status, "
         "to_char(date_created, 'YYYY-MM-DD HH24:MI:SS') FROM users_blacklist")
    out = subprocess.check_output(
        ["psql", "-h", config.PROD_PG["host"], "-p", str(config.PROD_PG["port"]),
         "-U", config.PROD_PG["user"], "-d", config.PROD_PG["dbname"],
         "--set=sslmode=require", "-t", "-A", "-F", "\t",
         "-c", "SET default_transaction_read_only = on;", "-c", q],
        env=env, text=True,
    )
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        f = line.split("\t")
        if len(f) < 10:
            continue
        f = [None if x in ("", "\\N") else x for x in f]
        rows.append(tuple(f[:10]))
    cur.executemany(
        "INSERT INTO bp_blacklist (id, blacklist_type, source, risk_level, name, entity_type, "
        "identifier_type, identifier, status, date_created) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, status=EXCLUDED.status",
        rows,
    )
    print(f"[rules] mirrored {len(rows)} blacklist entries into bp_blacklist")


if __name__ == "__main__":
    conn = db.connect()
    cur = conn.cursor()
    load_rules(cur)
    mirror_blacklist(cur)
    conn.commit()
    conn.close()
