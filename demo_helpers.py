#!/usr/bin/env python3
"""
Demo scenes used by demo_end_to_end.sh. Each subcommand prints a short,
human-readable narration of one capability against the REAL data in the test DB.

    python demo_helpers.py <scene>
    scenes: config | show_profile | rules_demo | velocity_demo | coldstart_demo | counts
"""
import csv
import json
import os
import sys
from datetime import datetime


import config
import db


def _conn():
    return db.connect()


def money(v):
    try:
        return f"NGN {float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def scene_config():
    print(f"  learning window     : last {config.LOOKBACK_MONTHS} months (quarterly)")
    print(f"  time-decay half-life: {config.DECAY_HALF_LIFE_DAYS} days  (weights ~1.0/0.8/0.5/0.2 at 0/30/90/180d)")
    print(f"  eligibility gate    : tenure >= {config.ELIGIBLE_MIN_TENURE_DAYS} days AND "
          f">= {config.ELIGIBLE_MIN_TXNS} clean lifetime txns")
    print(f"  learn from clean    : {'ONLY clean txns (exclude suspicious/blocked/blacklisted)' if config.LEARN_FROM_CLEAN_ONLY else 'all txns'}")
    print(f"  trust cutoff        : confidence >= {config.CONFIDENCE_TRUST_THRESHOLD}/100 to be trusted")
    print(f"  sanity amount cap   : {config.MAX_SANE_AMOUNT:,.0f} NGN (bigger = treated as bad data)")
    print(f"  production access   : READ-ONLY  ({config.PROD_PG['host']})")
    print(f"  profiles saved to   : PostgreSQL profile store ({config.STORE_PG['host']}:{config.STORE_PG['port']}/{config.STORE_PG['dbname']})")


def scene_governance():
    """Show the Active vs Warming-Up split + confidence bands (the porous-fix)."""
    conn = _conn(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bp_user_behaviour_profile")
    tot = cur.fetchone()[0] or 1
    print("  Only accounts with enough CLEAN history are trusted; the rest are 'Warming Up'")
    print("  and judged against similar customers (peers) — so fake activity can't become 'normal'.\n")
    cur.execute("SELECT profile_status, COUNT(*) FROM bp_user_behaviour_profile GROUP BY profile_status")
    for st, n in cur.fetchall():
        print(f"    {st:12s}: {n:>8,}  ({100*n/tot:.1f}%)")
    print("\n  confidence score distribution:")
    cur.execute("""SELECT CASE WHEN confidence_score>=80 THEN 'a 80-100 high'
                                WHEN confidence_score>=60 THEN 'b 60-79 trusted'
                                WHEN confidence_score>=40 THEN 'c 40-59 learning'
                                ELSE 'd 0-39 low' END band, COUNT(*)
                   FROM bp_user_behaviour_profile GROUP BY band ORDER BY band""")
    for band, n in cur.fetchall():
        print(f"    {band[2:]:16s}: {n:>8,}")
    conn.close()


def scene_warming():
    """Show one Warming-Up account — not trusted, judged by peers."""
    conn = _conn(); cur = db.dict_cursor(conn)
    cur.execute("""SELECT * FROM bp_user_behaviour_profile
                   WHERE profile_status='warming_up' AND total_tx_count BETWEEN 2 AND 20
                   ORDER BY total_tx_count DESC LIMIT 1""")
    p = cur.fetchone() or {}
    if p:
        print(f"  account {p['entity_key']} ({p['customer_name']})")
        print(f"    status = {p['profile_status'].upper()},  confidence = {p['confidence_score']}/100")
        print(f"    tenure = {p['tenure_days']} days,  clean lifetime txns = {p['lifetime_clean_txns']}")
        print("    -> too little history to trust; judged against its PEER GROUP, not itself.")
    conn.close()


def _pick_rich_profile(cur):
    # prefer a TRUSTED (Active) customer with real city names
    cur.execute(
        r"""SELECT * FROM bp_user_behaviour_profile
            WHERE profile_status='active' AND max_amount > 0
              AND JSON_LENGTH(usual_cities) >= 2
              AND usual_cities NOT LIKE '%"-"%' AND usual_cities NOT LIKE '%N/A%'
            ORDER BY confidence_score DESC, total_tx_count DESC LIMIT 1"""
    )
    p = cur.fetchone()
    if p:
        return p
    cur.execute(
        r"""SELECT * FROM bp_user_behaviour_profile
            WHERE total_tx_count BETWEEN 40 AND 400 AND max_amount > 0
              AND JSON_LENGTH(usual_cities) >= 2
              AND usual_cities NOT LIKE '%"-"%' AND usual_cities NOT LIKE '%N/A%'
            ORDER BY total_tx_count DESC LIMIT 1"""
    )
    p = cur.fetchone()
    if p:
        return p
    cur.execute(
        """SELECT * FROM bp_user_behaviour_profile
           WHERE total_tx_count BETWEEN 40 AND 400 AND usual_cities <> '{}' AND max_amount > 0
           ORDER BY total_tx_count DESC LIMIT 1"""
    )
    return cur.fetchone()


def scene_show_profile():
    conn = _conn(); cur = db.dict_cursor(conn)
    p = _pick_rich_profile(cur)
    cities = list(json.loads(p["usual_cities"] or "{}"))[:6]
    types = json.loads(p["usual_transaction_types"] or "{}")
    print(f"  customer (account)  : {p['entity_key']}   name: {p['customer_name']}")
    print(f"  status / confidence : {str(p.get('profile_status','?')).upper()}  ({p.get('confidence_score')}/100)  "
          f"tenure {p.get('tenure_days')}d, clean lifetime txns {p.get('lifetime_clean_txns')}")
    print(f"  learned from        : {p['total_tx_count']} of their own CLEAN transactions")
    print(f"  usual spend         : avg {money(p['avg_amount'])}, typical {money(p['median_amount'])}")
    print(f"  biggest ever        : {money(p['max_amount'])}   (95% below {money(p['p95_amount'])})")
    print(f"  recency-weighted    : {money(p['decayed_avg_amount'])}")
    print(f"  usual cities        : {', '.join(cities) or '(n/a)'}")
    print(f"  usual types         : {', '.join(f'{k} x{v}' for k, v in list(types.items())[:4])}")
    days = json.loads(p.get("peak_transaction_days") or "{}")
    busy_days = ", ".join(sorted(days, key=days.get, reverse=True)[:3])
    print(f"  busiest days        : {busy_days or '(n/a)'}  (most active: {p.get('top_day_of_week') or '?'})")
    print(f"  known beneficiaries : {p['distinct_beneficiaries']} accounts paid before")
    conn.close()


def _txn(p, **kw):
    d = dict(
        transaction_id="DEMO", branch_id=p["branch_id"], origin_account_no=p["origin_account_no"],
        account_type="individual", identifier=p["identifier"], amount=float(p["median_amount"] or 1000),
        currency="NGN", destination_account_no=None, customer_location="x, y, z",
        origin_country="NG", destination_country="NG", ts=datetime.now(),
    )
    d.update(kw)
    return d


def scene_rules_demo():
    from rule_engine import RuleEngine
    conn = _conn(); cur = db.dict_cursor(conn)
    p = _pick_rich_profile(cur)
    eng = RuleEngine(conn)
    known_city = list(json.loads(p["usual_cities"] or "{}"))[0]
    known_benef = next(iter(json.loads(p["beneficiaries"] or "{}")), "111")
    usual_hour = int(p["top_hour"]) if p["top_hour"] is not None else 13

    print(f"  customer: {p['entity_key']} ({p['customer_name']}), usual city {known_city}\n")
    normal = _txn(p, transaction_id="DEMO-NORMAL", destination_account_no=known_benef,
                  customer_location=f"a, {known_city}, b", amount=float(p["median_amount"] or 1000),
                  ts=datetime.now().replace(hour=usual_hour))
    fired = eng.evaluate(normal)
    print(f"  A) NORMAL txn ({money(normal['amount'])} to a known account, in {known_city}):")
    print("     -> " + ("no rules fired — recognised as this customer's normal ✔" if not fired
                        else ", ".join(f['rule_code'] for f in fired)))

    abn = _txn(p, transaction_id="DEMO-ABNORMAL", amount=float(p["max_amount"]) * 40 + 2_000_000_000,
               destination_account_no="9999999999", customer_location="road, Pyongyang, DPRK",
               destination_country="KP", ts=datetime.now().replace(hour=3))
    fired = eng.evaluate(abn)
    print(f"\n  B) ABNORMAL txn ({money(abn['amount'])}, new account, Pyongyang, 3am, cross-border):")
    for f in fired:
        print(f"     FIRED [{f['severity']:>8}] {f['rule_code']}")
    conn.close()


def scene_velocity_demo():
    from rule_engine import RuleEngine
    from live_velocity import CsvVelocitySource
    os.makedirs(config.DATA_DIR, exist_ok=True)
    burst = os.path.join(config.DATA_DIR, "_demo_burst.csv")
    rows = [("date_created", "branch_id", "origin_account_no", "destination_account_no", "amount", "origin_country")]
    for sec, dest in [("00", "D1"), ("08", "D2"), ("16", "D3"), ("24", "D4"), ("32", "D5")]:
        rows.append((f"2026-07-05 12:00:{sec}.000000+00", 231, "888", dest, "3000000", "NG"))
    with open(burst, "w", newline="") as f:
        csv.writer(f).writerows(rows)

    conn = _conn()
    eng = RuleEngine(conn, velocity=CsvVelocitySource(burst))
    txn = dict(transaction_id="DEMO-BURST", branch_id=231, origin_account_no="888", account_type="individual",
               identifier=None, amount=3_000_000, currency="NGN", destination_account_no="D6",
               customer_location="x, Lagos, y", origin_country="NG", destination_country="NG",
               ts=datetime(2026, 7, 5, 12, 0, 33))
    print("  scenario: account 888 sent 5 transactions in 32 seconds; a 6th arrives now.")
    print("  (the nightly profile can't see this — the LIVE look-up does)\n")
    for f in eng.evaluate(txn):
        if "velocity" in f["rule_code"] or "frequency" in f["rule_code"] or "15m" in f["rule_code"] or "recipient" in f["rule_code"]:
            print(f"     FIRED [{f['severity']:>8}] {f['rule_code']}  {f['details']}")
    conn.close()
    os.remove(burst)


def scene_coldstart_demo():
    from rule_engine import RuleEngine
    conn = _conn(); cur = db.dict_cursor(conn)
    cur.execute("SELECT usual_cities FROM bp_peer_baseline WHERE branch_id=231 AND account_type='individual' LIMIT 1")
    row = cur.fetchone()
    known_city = list(json.loads(row["usual_cities"] or "{}"))[0] if row else "Lagos"
    eng = RuleEngine(conn)
    new = dict(transaction_id="DEMO-NEW", branch_id=231, origin_account_no="9000000000001",
               account_type="individual", identifier=None, amount=5000, currency="NGN",
               destination_account_no="123", customer_location=f"x, {known_city}, y",
               origin_country="NG", destination_country="NG", ts=datetime.now().replace(hour=13))
    print("  a BRAND-NEW account (no history) — judged against its peer group:\n")
    f1 = eng.evaluate(new)
    print(f"  A) small normal txn in {known_city}: "
          + ("no rules fired — normal for a new account ✔" if not f1 else ", ".join(x['rule_code'] for x in f1)))
    new2 = dict(new, transaction_id="DEMO-NEW2", amount=900_000_000,
                customer_location="x, Pyongyang, y", destination_country="KP",
                ts=datetime.now().replace(hour=3))
    print("  B) first txn is 900,000,000 in Pyongyang at 3am:")
    for f in eng.evaluate(new2):
        b = f["details"].get("basis", "hard-rule")
        print(f"     FIRED [{f['severity']:>8}] {f['rule_code']}  ({b})")
    conn.close()


def scene_counts():
    conn = _conn(); cur = conn.cursor()
    for t, label in [
        ("bp_user_behaviour_profile", "customer profiles learned"),
        ("bp_peer_baseline", "peer baselines (cold-start)"),
        ("bp_rule_definition", "AML rules loaded"),
        ("bp_rule_settings", "per-client threshold overrides"),
        ("bp_blacklist", "blacklist entries"),
        ("bp_rule_event", "rule firings logged"),
    ]:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {label:32s}: {cur.fetchone()[0]:>8,}")
    conn.close()


SCENES = {
    "config": scene_config, "governance": scene_governance, "warming": scene_warming,
    "show_profile": scene_show_profile, "rules_demo": scene_rules_demo,
    "velocity_demo": scene_velocity_demo, "coldstart_demo": scene_coldstart_demo, "counts": scene_counts,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in SCENES:
        print("usage: python demo_helpers.py <" + "|".join(SCENES) + ">")
        raise SystemExit(1)
    SCENES[sys.argv[1]]()
