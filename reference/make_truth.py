"""reference/make_truth.py -- compute the ground truth for one database, and freeze it.

    & "C:\\Users\\Ting\\anaconda3\\python.exe" reference\\make_truth.py
    & "C:\\Users\\Ting\\anaconda3\\python.exe" reference\\make_truth.py scenarios\\S01.sqlite reference\\truth\\S01.json

Run it BEFORE the agent sees the database, commit the JSON, and never edit the
JSON by hand after looking at agent output. Eval cases take `expected` from here.

For the untouched snapshot the planted-driver list is empty. A scenario generator
will write what it planted into `planted`; this script records what the numbers
then look like, so a case can require both "names the planted driver" and
"quotes the right figures".
"""
from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference import kpi as K_mod                     # noqa: E402
from reference.kpi import KPI, DIMENSIONS               # noqa: E402


def r(x, nd=2):
    return round(float(x), nd)


def build(db_path: Path, planted: list | None = None) -> dict:
    k = KPI(db_path)
    last, last3 = k.last_complete_months(1)[0], k.last_complete_months(3)
    prev1, prev3 = k.previous_period(last)[0], k.previous_period(last3)
    m = k.monthly()

    def contrib(prev, cur, top=5):
        out = {}
        for dim in DIMENSIONS:
            g = k.contribution(dim, prev, cur).head(top)
            out[dim] = [{"segment": s, "delta": r(x["delta"]), "share_of_change": r(x["share_of_change"], 4),
                         "share_of_base": r(x["share_of_base"], 4), "lift": r(x["lift"], 3)}
                        for s, x in g.iterrows()]
        return out

    def decomp(prev, cur):
        d = k.decompose(prev, cur)
        return {kk: (r(v, 4) if kk.endswith(("_share", "_pct")) else r(v) if isinstance(v, float) else v)
                for kk, v in d.items()}

    return {
        "_meta": {
            "db": str(Path(db_path).resolve().relative_to(ROOT)).replace("\\", "/"),
            "db_sha256": hashlib.sha256(Path(db_path).read_bytes()).hexdigest(),
            "today": k.today,
            "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "definitions": "reference/KPI_DEFINITIONS.md v1",
            "thresholds": {"unusual_z": K_mod.UNUSUAL_Z, "zscore_lookback": K_mod.ZSCORE_LOOKBACK,
                           "driver_min_share": K_mod.DRIVER_MIN_SHARE,
                           "driver_min_lift_gap": K_mod.DRIVER_MIN_LIFT_GAP,
                           "min_total_change": K_mod.MIN_TOTAL_CHANGE},
        },
        "planted": planted or [],
        "defaults": {"last_month": last, "previous_month": prev1, "last_3_months": last3, "previous_3_months": prev3},
        "monthly": {mon: {"revenue": r(x["revenue"]), "net_revenue": r(x["net_revenue"]), "orders": int(x["orders"]),
                          "items": int(x["items"]), "aov": r(x["aov"]), "margin": r(x["margin"], 4),
                          "mom": r(x["mom"], 4)}
                         | {kk + "_share": r(v, 4) for kk, v in k.leakage(mon).items()}          # R21
                    for mon, x in m.tail(12).iterrows()},
        "last_month": {
            "period": last,
            "headline": k.period(last) | {"months": [last]},
            "vs_previous_month": decomp(prev1, last),
            "zscore": r(k.zscore(last), 2),
            "unusual": k.is_unusual(last),
            "drivers": k.find_drivers(prev1, last),
            "net_drivers": k.find_drivers(prev1, last, measure="net_revenue"),
            "contribution_top5": contrib(prev1, last),
            "funnel_by_browser": {s: {"sessions": int(x["sessions"]), "purchase_rate": r(x["purchase_rate"], 4)}
                                  for s, x in k.funnel(last, by="browser").iterrows()},
            "funnel_by_session_source": {s: {"sessions": int(x["sessions"]), "purchase_rate": r(x["purchase_rate"], 4)}
                                         for s, x in k.funnel(last, by="traffic_source").iterrows()},
            "leakage": {kk: r(v, 4) for kk, v in k.leakage(last).items()},
            "leakage_by_dist_center": {s: {"revenue": r(x["revenue"]), "cancelled": r(x["cancelled"], 4),
                                           "returned": r(x["returned"], 4)}
                                       for s, x in k.leakage(last, by="dist_center").iterrows()},
            "leakage_by_dist_center_previous_month": {s: r(x["returned"], 4)
                                                      for s, x in k.leakage(prev1, by="dist_center").iterrows()},
        },
        "last_3_months": {
            "period": last3,
            "headline": k.period(last3),
            "vs_previous_3_months": decomp(prev3, last3),
            "zscore_by_month": {mon: r(k.zscore(mon, before=last3[0]), 2) for mon in last3},
            "unusual_months": [mon for mon in last3 if k.is_unusual(mon, before=last3[0])],
            "drivers": k.find_drivers(prev3, last3),
            "net_drivers": k.find_drivers(prev3, last3, measure="net_revenue"),
            "contribution_top5": contrib(prev3, last3),
        },
        # Answers a careless agent gives. A case can recognise WHICH mistake was made.
        "known_wrong_answers": {
            "revenue_last_month_by_item_month": r(k.con.execute(
                "SELECT SUM(sale_price) FROM order_items WHERE substr(created_at,1,7)=?", (last,)).fetchone()[0]),
            "revenue_last_month_net_instead_of_gross": r(k.net_revenue(last)),
            "revenue_last_month_fanout": r(k.con.execute(
                """SELECT SUM(oi.sale_price) FROM order_items oi JOIN orders o USING(order_id)
                   JOIN order_items oi2 USING(order_id) WHERE substr(o.created_at,1,7)=?""", (last,)).fetchone()[0]),
            "aov_last_3_months_as_mean_of_monthly": r(sum(k.aov(mon) for mon in last3) / 3),
        },
    }


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else K_mod.DEFAULT_DB
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "reference" / "truth" / "base.json"
    truth = build(db)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    lm, l3 = truth["last_month"], truth["last_3_months"]
    print(f"wrote {out.relative_to(ROOT)}")
    print(f"  last month {lm['period']}: revenue {lm['headline']['revenue']:,.0f}, "
          f"z {lm['zscore']:+.2f}, unusual={lm['unusual']}, drivers={len(lm['drivers'])}")
    print(f"  last 3 months: revenue {l3['headline']['revenue']:,.0f}, "
          f"unusual months {l3['unusual_months']}, drivers={len(l3['drivers'])}")
    print(f"  known wrong answers: {truth['known_wrong_answers']}")


if __name__ == "__main__":
    main()
