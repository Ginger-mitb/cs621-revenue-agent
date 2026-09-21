"""baselines/script.py -- the recursive drill-down SCRIPT baseline (no LLM).

    & "C:\\Users\\Ting\\anaconda3\\python.exe" baselines\\script.py                 # base db, naive
    & "C:\\Users\\Ting\\anaconda3\\python.exe" baselines\\script.py scenarios\\S02.sqlite --thorough

It answers ONE fixed question, "why did revenue change last month?" (R8/R9
defaults), by walking the metric tree with reference/kpi.py. It cannot read a
question, so every other phrasing is out of scope for it by construction.

Two variants, because "how good is the script" is a design choice and the eval
must say which one it beat:
    naive     revenue -> unusual? (R10) -> gross drivers by every dimension (R11)
    thorough  + net-revenue drivers, returned/cancelled share vs its own history,
                returned share by distribution centre (R21)

The output follows evals/cases.py OUTPUT_CONTRACT (a `drivers` list; empty
means "no driver"), so one grader serves the script and the agent alike.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference import kpi as K_mod   # noqa: E402
from reference.kpi import KPI        # noqa: E402

LEAKAGE_HISTORY = 12      # months of returned-share history the thorough script compares against
LEAKAGE_SIGMA = 3.0       # flag the month when its returned share is this many sd above that history
SEGMENT_RATIO = 2.0       # flag a segment whose returned share is this many times the others' median


def run(db_path, thorough: bool = False) -> dict:
    k = KPI(db_path)
    last = k.last_complete_months(1)[0]
    prev = k.previous_period(last)[0]
    d = k.decompose(prev, last)
    z = k.zscore(last)
    out = {
        "period": [last], "previous": [prev], "measure": "revenue",
        "value": round(d["revenue_cur"], 2), "value_previous": round(d["revenue_prev"], 2),
        "change_pct": round(d["delta_pct"], 4), "zscore": round(z, 2), "unusual": bool(abs(z) >= K_mod.UNUSUAL_Z),
        "volume_share": round(d["volume_share"], 3), "aov_share": round(d["aov_share"], 3),
        "drivers": [],
        "assumptions": ["fixed question: why did revenue change last month? (R8/R9 defaults)"],
        "answer": "",
    }
    notes = []
    out["drivers"] += k.find_drivers(prev, last)                                   # R11 on gross
    if not out["unusual"]:
        notes.append(f"{last} is within normal variation (z {z:+.2f}); drivers listed are segments "
                     "that moved disproportionately, not an explanation of an anomaly")
    if not thorough:
        out["answer"] = " ".join(notes)
        return out

    # net revenue: the same rule, so the same R11 noise
    out["drivers"] += k.find_drivers(prev, last, measure="net_revenue")
    # leakage vs its own history
    months = k.months()
    hist = [k.leakage(m)["returned"] for m in months[-LEAKAGE_HISTORY - 1:-1]]
    mean = sum(hist) / len(hist)
    sd = (sum((x - mean) ** 2 for x in hist) / (len(hist) - 1)) ** 0.5
    cur = k.leakage(last)["returned"]
    out["returned_share"] = round(cur, 4)
    out["returned_share_history"] = {"mean": round(mean, 4), "sd": round(sd, 4), "months": LEAKAGE_HISTORY}
    if cur > mean + LEAKAGE_SIGMA * sd:
        notes.append(f"returned share {cur:.1%} is {(cur - mean) / sd:.1f} sd above its "
                     f"{LEAKAGE_HISTORY}-month mean {mean:.1%}")
        by = k.leakage(last, by="dist_center")["returned"]
        for seg, share in by.items():
            others = by.drop(seg).median()
            if share >= SEGMENT_RATIO * others:
                out["drivers"].append({"dimension": "dist_center", "segment": seg, "measure": "returned_share",
                                       "value": round(float(share), 4), "others_median": round(float(others), 4)})
    out["answer"] = " ".join(notes)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default=str(K_mod.DEFAULT_DB))
    ap.add_argument("--thorough", action="store_true")
    a = ap.parse_args()
    print(json.dumps(run(a.db, a.thorough), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
