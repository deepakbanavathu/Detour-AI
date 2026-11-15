#!/usr/bin/env python3
"""Self-test of the verifier: on the synthetic fixture, each planted fault must fail and everything else must pass.
A known_issue counts as a fail here (the fault is still detected). Usage: check_expected.py REPORT EXPECTED"""
import json
import sys

rep = json.load(open(sys.argv[1]))["results"]
exp = json.load(open(sys.argv[2]))["expected"]
bad = []
for cid, want in exp.items():
    got = (rep.get(cid) or {}).get("status", "missing")
    got = "fail" if got == "known_issue" else got
    if got != want:
        bad.append(f"{cid}: expected {want}, got {got}")
print("\n".join(bad) if bad else f"verifier self-test: all {len(exp)} expectations met")
sys.exit(1 if bad else 0)
