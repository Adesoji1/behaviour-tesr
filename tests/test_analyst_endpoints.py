"""Regression tests for the analyst insight endpoints (learnt behaviour / behaviour-change / audit).

Covers the model-INDEPENDENT logic (histogram summarising, baseline view, the previous-vs-current
diff incl. added / removed_invalidated / decay note) and the HTTP contract (all three endpoints are
registered, documented, and require the X-Adhere-Key). The model-dependent paths
(learned_behaviour / behaviour_change / score_payload include_audit) are exercised against the real
model in the container; here we pin the pure pieces + the OpenAPI/security contract.

Run:  .venv-ml/bin/python tests/test_analyst_endpoints.py   (or: pytest tests/test_analyst_endpoints.py)
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root on path

from ml import config, serve


# ---- pure histogram summarising --------------------------------------------------------------
def test_top_hist_indices_and_labels():
    assert serve._top_hist([0, 0, 5, 3, 0, 0], top=3) == [2, 3]          # busiest hours, >0 only
    assert serve._top_hist([0, 0, 0, 0, 0, 0, 0], serve._DOW) == []      # all-zero -> nothing
    days = serve._top_hist([0.5, 0, 0, 0, 0, 0.3, 0], serve._DOW, top=3)
    assert days == ["Monday", "Saturday"]


def test_baseline_view_shape():
    b = {"amt_median": 40000, "amt_mean": 46586, "amt_p95": 196334, "amt_max": 200000, "amt_std": 44434,
         "hour_hist": [0] * 21 + [0.6, 0, 0], "dow_hist": [0.4, 0, 0, 0, 0, 0.3, 0],
         "locs": {"Lagos", "Abuja"}, "benefs": {"a", "b", "c"}, "types": {"transfer"}, "ips": {"1.2.3"}}
    v = serve._baseline_view(b)
    assert set(v) == {"amount", "usual_hours_of_day", "usual_days_of_week", "known_locations",
                      "known_beneficiaries_count", "known_transaction_types", "known_ip_subnets_count"}
    assert v["amount"]["median"] == 40000.0
    assert v["known_beneficiaries_count"] == 3
    assert v["usual_hours_of_day"] == [21]
    assert v["usual_days_of_week"] == ["Monday", "Saturday"]


# ---- the re-learning diff (added / removed_invalidated / decay) -------------------------------
def test_diff_baselines_added_removed_invalidated():
    prev = {"amt_median": 100, "amt_mean": 110, "amt_p95": 200, "amt_max": 300,
            "hour_hist": [], "dow_hist": [], "locs": {"Lagos", "Abuja"}, "benefs": {"a", "b"},
            "types": {"transfer"}}
    cur = {"amt_median": 150, "amt_mean": 160, "amt_p95": 250, "amt_max": 400,
           "hour_hist": [], "dow_hist": [], "locs": {"Lagos", "Kano"}, "benefs": {"b", "c"},
           "types": {"transfer", "airtime"}}
    d = serve._diff_baselines(prev, cur)
    assert d["locations"]["added"] == ["Kano"]
    assert d["locations"]["removed_invalidated"] == ["Abuja"]
    assert d["locations"]["still_known"] == ["Lagos"]
    assert d["beneficiaries"]["added"] == 1 and d["beneficiaries"]["removed_invalidated"] == 1
    assert d["transaction_types"]["added"] == ["airtime"]
    assert d["amount"]["median_shift"] == 50.0
    assert d["decay"]["half_life_days"] == config.DECAY_HALF_LIFE_DAYS


def test_score_payload_has_optional_audit_flag():
    params = inspect.signature(serve.score_payload).parameters
    assert "include_audit" in params
    assert params["include_audit"].default is False            # /score stays audit-free by default


# ---- HTTP contract: registered, documented, API-key protected --------------------------------
def test_endpoints_registered_documented_and_secured():
    try:
        import service
    except ImportError as e:
        print(f"  (skip: serving deps unavailable in this env — {e})")
        return                                                   # runs fully in the container/serving env
    schema = service.app.openapi()                              # must not raise (valid Swagger)
    paths = schema["paths"]
    for p, meth in [("/learned", "get"), ("/behaviour-change/{identifier}", "get"),
                    ("/score/audit", "post")]:
        assert p in paths, f"missing route {p}"
        op = paths[p][meth]
        assert op.get("security"), f"{p} not API-key protected"
        assert op.get("summary") and op.get("description"), f"{p} not documented"
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["name"] == "X-Adhere-Key"


if __name__ == "__main__":
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
