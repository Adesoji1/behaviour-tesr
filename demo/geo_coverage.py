#!/usr/bin/env python3
"""
Geo-velocity SHADOW coverage + signal report (phase2).

Reads the compliance inference log(s) (artifacts/inference_log/inference-*.jsonl), which carry the
per-transaction geo shadow telemetry, and reports everything phase2 asks us to measure:
coverage, resolution-source distribution, unresolved reasons, geo-velocity availability + distribution,
unusually-high-velocity cases, and whether geo anomalies overlap the existing review/unsafe/safe
decisions. Telemetry only — this reads logs; it changes nothing.

    docker exec adhere-behaviour python demo/geo_coverage.py [--high-kmh 900] [--glob '/app/artifacts/inference_log/inference-*.jsonl']
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics as st


def _pct(n, d):
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="/app/artifacts/inference_log/inference-*.jsonl")
    ap.add_argument("--high-kmh", type=float, default=900.0,
                    help="threshold for an 'unusually high' geo-velocity (default 900 km/h)")
    a = ap.parse_args()

    rows = []
    for path in sorted(glob.glob(a.glob)):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("event") == "inference" and isinstance(r.get("geo"), dict):
                    rows.append(r)

    if not rows:
        print("No geo-tagged inference rows found for", a.glob)
        return

    g = lambda r, k, d=None: (r.get("geo") or {}).get(k, d)
    eligible = [r for r in rows if g(r, "geo_eligible") is True or
                (g(r, "geo_eligible") is None and r.get("is_cold_start") is False)]
    N = len(rows)
    E = len(eligible)

    def src_dist(pop):
        from collections import Counter
        c = Counter(g(r, "geo_source", "unavailable") for r in pop)
        return c

    print("=" * 72)
    print("GEO-VELOCITY SHADOW — COVERAGE & SIGNAL REPORT")
    print("=" * 72)
    print(f"inference rows with geo telemetry : {N}")
    print(f"  of which ELIGIBLE (established)  : {E}  ({_pct(E, N)})")
    print(f"  cold-start / ineligible          : {N - E}  (geo not evaluated — unchanged behaviour)")

    print("\n-- Resolution source (ELIGIBLE transactions) --")
    dist = src_dist(eligible)
    usable = sum(v for k, v in dist.items() if k != "unavailable")
    for s in ("coordinates", "plus_code", "location_registry", "ip", "unavailable"):
        print(f"  {s:18} {dist.get(s,0):6}  ({_pct(dist.get(s,0), E)})")
    print(f"  USABLE coordinates {usable:6}  ({_pct(usable, E)})   <- % eligible txns with usable coords")

    # -- Plus Codes (phase3) --
    pc_present = sum(1 for r in eligible if g(r, "plus_code_present"))
    pc_decoded = sum(1 for r in eligible if g(r, "plus_code_decoded"))
    print("\n-- Plus Codes (ELIGIBLE) --")
    print(f"  transactions CONTAINING a Plus Code : {pc_present}  ({_pct(pc_present, E)})")
    print(f"  Plus Codes successfully DECODED     : {pc_decoded}  ({_pct(pc_decoded, pc_present)} of present)")

    def _prec(pop, src):
        vals = [float(g(r, "geo_precision_m")) for r in pop
                if g(r, "geo_source") == src and g(r, "geo_precision_m") is not None]
        return vals
    pc_prec = _prec(eligible, "plus_code")
    reg_prec = _prec(eligible, "location_registry")
    print("\n-- Geographic PRECISION (metres; smaller = finer) --")
    if pc_prec:
        print(f"  plus_code       n={len(pc_prec):5} median={st.median(pc_prec):.1f}m  max={max(pc_prec):.1f}m")
    if reg_prec:
        print(f"  city/state reg  n={len(reg_prec):5} median={st.median(reg_prec):.0f}m (centroid — coarse)")
    if pc_prec and reg_prec:
        print(f"  --> Plus Codes are ~{st.median(reg_prec)/max(1e-9,st.median(pc_prec)):.0f}x finer than centroids")

    unknown_loc = sum(1 for r in eligible if g(r, "loc_present") and not g(r, "loc_resolved"))
    bad_ip = sum(1 for r in eligible if g(r, "ip_present") and not g(r, "ip_public"))
    print("\n-- Unresolved detail (ELIGIBLE) --")
    print(f"  unknown/ambiguous location (present but unresolved): {unknown_loc}  ({_pct(unknown_loc, E)})")
    print(f"  invalid/private/malformed IP                       : {bad_ip}  ({_pct(bad_ip, E)})")
    from collections import Counter
    reasons = Counter(g(r, "unresolved_reason") for r in eligible if g(r, "geo_source") == "unavailable")
    for reason, c in reasons.most_common():
        print(f"    reason={reason}: {c}")

    vel_rows = [r for r in eligible if g(r, "geo_velocity_available") == 1 and g(r, "geo_velocity_kmh") is not None]
    kmh = [float(g(r, "geo_velocity_kmh")) for r in vel_rows]
    print("\n-- Geo-velocity availability & distribution (ELIGIBLE) --")
    print(f"  geo-velocity CALCULABLE (prev+current point): {len(vel_rows)}  ({_pct(len(vel_rows), E)})")
    if kmh:
        ks = sorted(kmh)
        q = lambda p: ks[min(len(ks) - 1, int(p * len(ks)))]
        print(f"  km/h  min={ks[0]:.1f}  median={st.median(ks):.1f}  p90={q(0.9):.1f}  "
              f"p95={q(0.95):.1f}  max={ks[-1]:.1f}")
        nonzero = [v for v in kmh if v > 0]
        print(f"  NON-ZERO geo-velocity observations: {len(nonzero)}  ({_pct(len(nonzero), len(kmh))} of calculable)")
        high = [r for r in vel_rows if float(g(r, "geo_velocity_kmh")) >= a.high_kmh]
        print(f"  unusually high (>= {a.high_kmh:.0f} km/h): {len(high)}")

        print("\n-- Do geo anomalies overlap the model's existing decisions? --")
        by_status = Counter(r.get("status") for r in high)
        for stt in ("unsafe", "review", "safe"):
            print(f"  high-velocity txns with status={stt:7}: {by_status.get(stt,0)}")
        safe_high = by_status.get("safe", 0)
        print(f"  --> high geo-velocity in transactions the model calls SAFE: {safe_high} "
              f"({_pct(safe_high, len(high))} of high-velocity)   <- potential NEW signal")
    else:
        print("  (no calculable geo-velocity yet — need >=2 resolvable points per eligible customer)")

    print("\n-- Verdict input --")
    print(f"  usable-coordinate coverage on eligible traffic: {_pct(usable, E)}")
    print("  (phase2 gate: is this high enough, and does geo-velocity flag anomalies the 27 features")
    print("   miss? If coverage stays low or high-velocity fully overlaps existing review/unsafe,")
    print("   geo-velocity is NOT yet justified as an ML feature.)")


if __name__ == "__main__":
    main()
