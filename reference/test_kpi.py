"""reference/test_kpi.py -- is the referee itself right?

    & "C:\\Users\\Ting\\anaconda3\\python.exe" reference\\test_kpi.py        (or: pytest reference)

Four kinds of check, none of which needs the agent:

    CROSS-PATH   kpi.py (pandas) and queries/*.sql (SQL) were written separately
                 and must return the same numbers
    INVARIANT    identities that hold whatever the right answer is
    REGRESSION   numbers pinned for THIS snapshot; if one moves, either the
                 snapshot or a definition changed, and the truth files are stale
    DATA QUIRK   odd facts about theLook that the definitions rely on
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from reference.kpi import KPI, DIMENSIONS, SESSION_DIMENSIONS, UNUSUAL_Z  # noqa: E402

K = KPI()
QUERIES = Path(__file__).parent / "queries"
DIM_SQL = {"category": "p.category", "brand": "p.brand", "department": "p.department",
           "dist_center": "dc.name", "acq_source": "u.traffic_source", "country": "u.country"}
RECENT = ["2026-06", "2026-07", "2026-08"]


def sql(name: str, dim: str | None = None, **params) -> pd.DataFrame:
    text = (QUERIES / f"{name}.sql").read_text(encoding="utf-8")
    if "{dim_expr}" in text:
        allowed = {**DIM_SQL, **{d: d for d in SESSION_DIMENSIONS}}
        text = text.replace("{dim_expr}", allowed[dim])          # whitelist only
    return pd.read_sql(text, K.con, params=params)


def close(a, b, rel=1e-9) -> bool:
    return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=1e-6)


# ------------------------------------------------------------------ CROSS-PATH

def test_monthly_core_sql_equals_pandas():
    s = sql("monthly_core").set_index("month")
    p = K.monthly()
    s = s.loc[p.index]                                     # complete months only
    for col in ("revenue", "net_revenue", "orders", "items", "profit"):
        bad = [m for m in p.index if not close(s.loc[m, col], p.loc[m, col])]
        assert not bad, f"{col}: SQL and pandas disagree in {bad[:5]}"


def test_revenue_by_dim_sql_equals_pandas():
    for dim in DIM_SQL:
        for month in RECENT:
            s = sql("revenue_by_dim", dim=dim, month=month).set_index("segment")["revenue"]
            p = K.fact[K.fact["month"] == month].groupby(dim)["sale_price"].sum()
            assert set(s.index) == set(p.index), f"{dim} {month}: different segments"
            bad = [k for k in p.index if not close(s[k], p[k])]
            assert not bad, f"{dim} {month}: SQL and pandas disagree for {bad[:5]}"


def test_customer_type_sql_equals_pandas():
    for month in RECENT:
        s = sql("revenue_by_customer_type", month=month).set_index("segment")["revenue"]
        p = K.fact[K.fact["month"] == month].groupby("customer_type")["sale_price"].sum()
        for seg in ("new", "returning"):
            assert close(s[seg], p[seg]), f"{month} {seg}: {s[seg]} vs {p[seg]}"


def test_funnel_and_leakage_sql_equals_pandas():
    for month in RECENT:
        for dim in SESSION_DIMENSIONS:
            s = sql("funnel_by_dim", dim=dim, month=month).set_index("segment")
            p = K.funnel(month, by=dim)
            for seg in p.index:
                assert int(s.loc[seg, "sessions"]) == int(p.loc[seg, "sessions"])
                assert close(s.loc[seg, "purchase_rate"], p.loc[seg, "purchase_rate"])
        s, p = sql("leakage", month=month).iloc[0], K.leakage(month)
        assert close(s["cancelled"], p["cancelled"]) and close(s["returned"], p["returned"])


# ------------------------------------------------------------------ INVARIANT

def test_fact_has_one_row_per_order_item():
    n = K.con.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
    assert len(K.fact) == n, f"fact has {len(K.fact)} rows, order_items has {n}: a join dropped or duplicated rows"
    assert K.fact["item_id"].is_unique


def test_segments_sum_to_total():
    for dim in DIMENSIONS:
        for month in RECENT:
            seg = K.fact[K.fact["month"] == month].groupby(dim)["sale_price"].sum().sum()
            assert close(seg, K.revenue(month)), f"{dim} {month}: segments do not add up to the total"


def test_decomposition_adds_up():
    pairs = [("2026-07", "2026-08"), ("2026-01", "2026-02"), (["2026-03", "2026-04", "2026-05"], RECENT)]
    for prev, cur in pairs:
        d = K.decompose(prev, cur)
        assert close(d["volume_effect"] + d["aov_effect"], d["delta"]), f"{prev}->{cur}: effects do not sum to delta"
        assert close(d["volume_share"] + d["aov_share"], 1.0)


def test_contribution_shares_sum_to_one():
    for dim in DIMENSIONS:
        g = K.contribution(dim, "2026-07", "2026-08")
        assert close(g["share_of_change"].sum(), 1.0) and close(g["share_of_base"].sum(), 1.0), dim
        assert close(g["delta"].sum(), K.revenue("2026-08") - K.revenue("2026-07")), dim


def test_net_measure_and_leakage_by_segment_add_up():
    """R4/R21. Net contribution shares sum to one and its delta is the net change;
    per-segment leakage is revenue-weighted, so returned x revenue sums to returned revenue."""
    g = K.contribution("dist_center", "2026-07", "2026-08", measure="net_revenue")
    assert close(g["share_of_change"].sum(), 1.0)
    assert close(g["delta"].sum(), K.net_revenue("2026-08") - K.net_revenue("2026-07"))
    lk = K.leakage("2026-08", by="dist_center")
    f = K.fact[K.fact["month"] == "2026-08"]
    assert close(lk["revenue"].sum(), K.revenue("2026-08"))
    assert close((lk["returned"] * lk["revenue"]).sum(), f.loc[f["order_status"] == "Returned", "sale_price"].sum())
    assert close((lk["cancelled"] * lk["revenue"]).sum(), f.loc[f["order_status"] == "Cancelled", "sale_price"].sum())


def test_months_sum_to_full_period():
    total = K.con.execute("""SELECT SUM(oi.sale_price) FROM order_items oi JOIN orders o USING(order_id)
                             WHERE o.created_at < ?""", (K.today,)).fetchone()[0]
    assert close(K.monthly()["revenue"].sum(), total)


def test_period_aov_is_ratio_of_totals_not_mean_of_ratios():
    p = K.period(RECENT)
    assert close(p["aov"], p["revenue"] / p["orders"])
    mean_of_monthly = sum(K.aov(m) for m in RECENT) / 3
    assert not close(p["aov"], mean_of_monthly, rel=1e-6), "the two AOV definitions coincide; R6 would be untestable"


# ------------------------------------------------------------------ REGRESSION (this snapshot)

def test_snapshot_is_the_one_we_pinned():
    man = json.loads((ROOT / "data" / "snapshot" / "manifest.json").read_text())
    assert man["cutoff"] == K.today
    for table, meta in man["tables"].items():
        n = K.con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        assert n == meta["rows"], f"{table}: sqlite has {n} rows, manifest says {meta['rows']}. Rebuild with buildsql.py"


def test_pinned_numbers_aug_2026():
    assert round(K.revenue("2026-08")) == 519370          # R2/R3: gross, by ORDER month
    assert round(K.net_revenue("2026-08")) == 388487      # R4
    assert K.orders("2026-08") == 6150                    # R5
    assert round(K.aov("2026-08"), 2) == 84.45            # R6
    assert round(K.margin("2026-08"), 3) == 0.520         # R7
    assert round(K.mom("2026-08"), 3) == 0.203            # R9


def test_default_period_and_previous_period():
    assert K.last_complete_months(1) == ["2026-08"]                       # R8
    assert K.last_complete_months(3) == RECENT
    assert K.previous_period(RECENT) == ["2026-03", "2026-04", "2026-05"]  # R9
    assert "2026-09" not in K.months()                                     # today's month is never complete


def test_unusual_rule():
    z = {m: K.zscore(m, before="2026-06") for m in RECENT}                 # one baseline for the whole window
    assert round(z["2026-08"], 2) == 2.06 and abs(z["2026-06"]) < UNUSUAL_Z and abs(z["2026-07"]) < UNUSUAL_Z
    assert K.is_unusual("2026-08", before="2026-06") and not K.is_unusual("2026-07", before="2026-06")


def test_aug_2026_is_a_knife_edge_do_not_build_a_case_on_it():
    """Asked on its own, Aug is judged against the 24 months before AUGUST (which
    include the strong Jun and Jul): z = 1.99, not unusual. Asked as part of the
    Jun-Aug window it is judged against the 24 months before JUNE: z = 2.06,
    unusual. Both follow R10. A truth that flips on 0.07 of a z-score is not a
    fair eval case; pick scenarios with a clear margin."""
    assert round(K.zscore("2026-08"), 2) == 1.99 and not K.is_unusual("2026-08")
    assert round(K.zscore("2026-08", before="2026-06"), 2) == 2.06


def test_null_brand_is_kept_as_unknown():
    n = K.con.execute("SELECT COUNT(*) FROM products WHERE brand IS NULL").fetchone()[0]
    assert n == 24, f"expected 24 products without a brand, found {n}"                # R17
    assert "(unknown)" in set(K.fact["brand"]) and K.fact["brand"].notna().all()


def test_base_snapshot_has_no_driver():
    """The untouched snapshot is pure organic growth. If this ever returns a
    driver, either R11's thresholds changed or someone perturbed the base."""
    assert K.find_drivers("2026-07", "2026-08") == []
    assert K.find_drivers(["2026-03", "2026-04", "2026-05"], RECENT) == []


def test_net_revenue_on_base_has_one_noise_driver():
    """R11 applied to NET revenue is noisier: Jul->Aug it flags Philadelphia PA
    (share 0.196, lift 2.24) although nothing was planted. On gross the same DC
    sits just under both thresholds (share 0.143, lift 1.45). Consequences: an
    eval case must never REQUIRE an explanation of Philadelphia, and a scenario's
    truth lists the planted driver separately from what the referee flags."""
    net = K.find_drivers("2026-07", "2026-08", measure="net_revenue")
    assert [(d["dimension"], d["segment"]) for d in net] == [("dist_center", "Philadelphia PA")]
    g = K.contribution("dist_center", "2026-07", "2026-08").loc["Philadelphia PA"]
    assert 0.14 < g["share_of_change"] < 0.15 and 1.4 < g["lift"] < 1.5


def test_unknown_month_and_dimension_fail_loudly():
    for bad in (lambda: K.revenue("2026-09"), lambda: K.revenue("2031-01"),
                lambda: K.contribution("colour", "2026-07", "2026-08"), lambda: K.funnel("2026-08", by="country")):
        try:
            bad()
        except ValueError as e:
            assert "available" in str(e) or "covers" in str(e), "error must say what IS valid"
        else:
            raise AssertionError("expected ValueError")


# ------------------------------------------------------------------ DATA QUIRK (the definitions rely on these)

def test_trap_item_month_differs_from_order_month():
    wrong = sql("trap_item_month", month="2026-08").iloc[0, 0]
    assert round(wrong) == 516172 and round(K.revenue("2026-08")) == 519370          # R2


def test_trap_fanout_double_counts():
    wrong = sql("trap_fanout", month="2026-08").iloc[0, 0]
    assert 1.8 < wrong / K.revenue("2026-08") < 2.0                                    # R16


def test_order_items_user_matches_order_user():
    n = K.con.execute("""SELECT COUNT(*) FROM order_items oi JOIN orders o USING(order_id)
                         WHERE oi.user_id <> o.user_id""").fetchone()[0]
    assert n == 0, f"{n} items carry a different user_id from their order"


def test_funnel_quirks():
    s = K.con.execute("SELECT SUM(purchased), COUNT(*), SUM(user_id IS NULL AND purchased = 1) FROM sessions").fetchone()
    items = K.con.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
    assert abs(s[0] - items) / items < 0.005, "R15: purchase sessions track order ITEMS, one each"
    assert s[2] == 0, "anonymous sessions never purchase"
    rate = dict(K.con.execute("SELECT substr(started_at,1,4), AVG(purchased) FROM sessions GROUP BY 1"))
    assert rate["2019"] < 0.05 and rate["2026"] > 0.45, "R15: purchase rate rises mechanically over the years"


def test_two_traffic_source_vocabularies():
    users = {r[0] for r in K.con.execute("SELECT DISTINCT traffic_source FROM users")}
    sess = {r[0] for r in K.con.execute("SELECT DISTINCT traffic_source FROM sessions")}
    assert users == {"Search", "Organic", "Facebook", "Display", "Email"}             # R14
    assert sess == {"Email", "Adwords", "YouTube", "Facebook", "Organic"}


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}\n      {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
