#!/usr/bin/env python3
"""
Manage the single active **X-Adhere-Key** for POST /score (behaviour service).

Only the SHA-256 **hash** is stored — never the plaintext. Exactly ONE key is active at a time:
`rotate` deactivates every previous key and activates a fresh one, so a rotated key immediately
invalidates the former. The new plaintext key is printed **once** — copy it then; it cannot be
recovered (only its hash is kept).

    python manage_api_key.py rotate [--label "adhere-prod 2026-08"]   # create + activate; invalidate old
    python manage_api_key.py show                                     # list keys (metadata only)
    python manage_api_key.py revoke                                   # deactivate all (/score -> 503)

The generated key is `secrets.token_hex(32)` — identical in strength/format to `openssl rand -hex 32`.
"""
import argparse
import hashlib
import secrets

import db


def _hash(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def rotate(label: str | None = None) -> None:
    key = secrets.token_hex(32)                       # == openssl rand -hex 32
    with db.connect() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE bp_api_key SET active = false, revoked_at = now() WHERE active")
        cur.execute("INSERT INTO bp_api_key (key_hash, label, active) VALUES (%s, %s, true)",
                    (_hash(key), label))
        conn.commit()
    print("\n  New X-Adhere-Key (shown ONCE — store it now, it cannot be recovered):\n")
    print(f"      {key}\n")
    print("  All previous keys are now INVALIDATED. Send it on every /score request as:")
    print("      -H 'X-Adhere-Key: <key>'\n")
    print("  The running service picks it up within ~30s, or immediately via  POST /reload.")


def show() -> None:
    with db.connect() as conn:
        cur = db.dict_cursor(conn)
        cur.execute("SELECT id, label, active, created_at, revoked_at "
                    "FROM bp_api_key ORDER BY id DESC LIMIT 20")
        rows = cur.fetchall()
    if not rows:
        print("no API keys yet — run:  python manage_api_key.py rotate")
        return
    for r in rows:
        print(f"  id={r['id']}  active={r['active']}  label={r['label'] or '-'}  "
              f"created={r['created_at']}  revoked={r['revoked_at'] or '-'}")


def revoke() -> None:
    with db.connect() as conn:
        conn.cursor().execute("UPDATE bp_api_key SET active = false, revoked_at = now() WHERE active")
        conn.commit()
    print("All keys revoked. /score will return 503 until you rotate a new one.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage the X-Adhere-Key for /score")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("rotate", help="create + activate a new key; invalidate the former")
    r.add_argument("--label", help="optional human label")
    sub.add_parser("show", help="list keys (metadata only)")
    sub.add_parser("revoke", help="deactivate all keys")
    a = ap.parse_args()
    db.ensure_schema()                                # make sure bp_api_key exists
    if a.cmd == "rotate":
        rotate(a.label)
    elif a.cmd == "show":
        show()
    elif a.cmd == "revoke":
        revoke()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
