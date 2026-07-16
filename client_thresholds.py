#!/usr/bin/env python3
"""
Per-client (per-institution) rule thresholds.

Anita's decision: thresholds differ by institution — a tier-1 bank, a tier-2 and
a tier-3 each want different limits — so each CLIENT sets their own. A client is a
`branch_id` in our data. This writes overrides into bp_rule_settings; the rule
engine reads them first and falls back to the global default when none is set.

As a function:
    from client_thresholds import set_threshold
    set_threshold(231, "block_above_hard_cap", params={"hard_cap": 250_000_000})
    set_threshold(231, "detect_unusual_city", enabled=False)   # switch a rule off

As a CLI:
    python client_thresholds.py --branch 231 --rule block_above_hard_cap --set hard_cap=250000000
    python client_thresholds.py --branch 231 --rule detect_unusual_city --disable
    python client_thresholds.py --list --branch 231
"""
import argparse
import json

import config
import db


def set_threshold(branch_id: int, rule_code: str, params: dict | None = None, enabled: bool | None = None):
    conn = db.connect()
    cur = conn.cursor()
    en = None if enabled is None else (1 if enabled else 0)
    cur.execute(
        "INSERT INTO bp_rule_settings (branch_id, rule_code, params, enabled) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (branch_id, rule_code) DO UPDATE SET "
        "params=EXCLUDED.params, enabled=EXCLUDED.enabled",
        (branch_id, rule_code, json.dumps(params) if params is not None else None, en),
    )
    conn.commit()
    conn.close()
    print(f"[set] branch={branch_id} rule={rule_code} params={params} enabled={enabled}")


def list_for(branch_id: int | None):
    conn = db.connect()
    cur = conn.cursor()
    if branch_id is None:
        cur.execute("SELECT branch_id, rule_code, params, enabled FROM bp_rule_settings ORDER BY branch_id, rule_code")
    else:
        cur.execute("SELECT branch_id, rule_code, params, enabled FROM bp_rule_settings WHERE branch_id=%s ORDER BY rule_code", (branch_id,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("(no client overrides set — every rule uses the global default)")
    for b, rc, prm, en in rows:
        state = "" if en is None else ("  [ENABLED]" if en else "  [DISABLED]")
        print(f"  branch {b} · {rc}: {prm or '{}'}{state}")


def _parse_kv(pairs: list[str]) -> dict:
    out = {}
    for kv in pairs:
        k, _, v = kv.partition("=")
        try:
            v = json.loads(v)          # numbers / bools / json
        except json.JSONDecodeError:
            pass                        # keep as string
        out[k] = v
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", type=int)
    ap.add_argument("--rule")
    ap.add_argument("--set", nargs="*", default=[], help="key=value threshold overrides")
    ap.add_argument("--enable", action="store_true")
    ap.add_argument("--disable", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        list_for(args.branch)
    elif args.branch and args.rule:
        enabled = True if args.enable else (False if args.disable else None)
        params = _parse_kv(args.set) if args.set else None
        set_threshold(args.branch, args.rule, params=params, enabled=enabled)
    else:
        ap.error("use --list, or provide --branch and --rule")
