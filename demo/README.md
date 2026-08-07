# Behavioural `/score` — decision review kit

Everything here lets you (and your boss) inspect a model decision **end-to-end** and confirm the
model is deciding from **learned history vs the real-time payload**, not guessing.

## Quick start
```bash
# stack up
docker compose up -d db redis behaviour-profile sync
# make your own payload from the sanitised template (payload.json is git-ignored — it holds a
# REAL customer identifier, so it is never committed):
cp demo/payload.example.json demo/payload.json      # then edit amount / customer / beneficiary
# review the decision for demo/payload.json:
./demo/run_audit.sh
# or a different payload file:
./demo/run_audit.sh demo/my_payload.json
```

> `demo/payload.example.json` is committed and uses **synthetic** values only (identifier
> `00000000000`, "Jane Doe") — safe to share. To exercise a customer's **personal** profile,
> copy it to `demo/payload.json` and substitute a real customer's details locally; that file stays
> git-ignored so no customer PII is committed.

## Files
| File | What it is |
|---|---|
| `payload.json` | The transaction to test. **Edit** it (amount, beneficiary, location, type, …). |
| `run_audit.sh` | Runs a full review: clears the customer's live-velocity window, prints the **Sanity Audit**, then calls the **live `/score`** and prints the actual decision. The two should match. |
| `decision_audit.py` | The audit logic — learned baseline vs the payload, features, detectors, blend, thresholds, decision, and the "why". Run standalone: `docker exec -i adhere-behaviour python demo/decision_audit.py < payload.json`. |
| `bf_test.py` / `bf_category_tests.txt` | A broader BF-category probe suite + observed results (amount sweep, velocity demo, category probes). |
| `demo_runner.py` | Light load runner — replays real transactions through `/score` (see main README). |

## Notes
- The API key is read automatically from `.env` (`BP_API_KEY`) — nothing to paste.
- **Between manual tests, `run_audit.sh` clears the customer's live-velocity window** so repeated
  probes don't accumulate velocity and skew the result. (If testing in Postman directly, run
  `docker exec adhere-redis redis-cli DEL vel:<identifier>` yourself between calls.)
- To test the *amount* behaviour cleanly, use the customer's **real** attributes (a real beneficiary,
  a real IP subnet, their real transaction type) so novelty flags don't fire — otherwise every
  placeholder value reads as "new" for that customer.
