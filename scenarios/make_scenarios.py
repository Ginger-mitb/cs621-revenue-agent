"""scenarios/make_scenarios.py -- plant nameable business events into copies of the base db.

    & "C:\\Users\\Ting\\anaconda3\\python.exe" scenarios\\make_scenarios.py              # every scenario
    & "C:\\Users\\Ting\\anaconda3\\python.exe" scenarios\\make_scenarios.py S01 S02      # some of them
    & "C:\\Users\\Ting\\anaconda3\\python.exe" scenarios\\make_scenarios.py --list

For each scenario: copy data/thelook.sqlite -> scenarios/<id>.sqlite, apply the edit,
cascade it through every table that would have noticed (orders -> order_items ->
inventory_items -> sessions), verify, and freeze the truth with reference/make_truth
into reference/truth/<id>.json. scenarios/manifest.json records what was planted.

Rules (group project/CLAUDE.md):
  * the base db is never modified; scenario dbs are derived and never committed
  * seeded: same code + same seed -> the same scenario db
  * plant far past the R10/R11 thresholds: share >= 30%, |lift - 1| >= 1, and |z| >= 3
    whenever the scenario moves the monthly total at all
  * every scenario is a business event someone could name
  * truth is frozen BEFORE any agent sees the db and never edited afterwards; the
    planted driver is listed separately from whatever the referee happens to flag

Why the numbers are what they are: August 2026 already grows +20% MoM in the base
(z 1.99), so a REMOVAL can never make the month unusual unless it takes a third of
the month away. A single-dimension driver that is also unusual therefore has to be a
surge (S01 clones orders). Net revenue is 75% of gross and its Jul->Aug change is
62k, so a returns problem has to hit 30%+ of that to clear R11 (S02 returns 80% of
one distribution centre's delivered orders, in August only: applying it from July
as first planned leaves Jul and Aug equally depressed and the Jul->Aug driver test
sees nothing).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import sys
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reference import kpi as K_mod        # noqa: E402
from reference import make_truth          # noqa: E402
from reference import test_kpi as T       # noqa: E402  (imports load the base db once)
from reference.kpi import KPI             # noqa: E402

BASE_DB = K_mod.DEFAULT_DB
SCEN_DIR = ROOT / "scenarios"
TRUTH_DIR = ROOT / "reference" / "truth"
MANIFEST = SCEN_DIR / "manifest.json"
SEED = 20260901
TS = "%Y-%m-%d %H:%M:%S"
MARGIN = {"share": 0.30, "lift_gap": 1.0, "z": 3.0}          # far past R11 (0.15 / 0.5) and R10 (2.0)
SHIPPED = ("Shipped", "Complete", "Returned")                   # statuses that carry shipped_at
DELIVERED = ("Complete", "Returned")

# test_kpi checks that are pinned to the untouched snapshot; every other test must
# also pass on a scenario db (cross-path, invariants, data quirks).
PINNED_TESTS = {
    "test_snapshot_is_the_one_we_pinned", "test_pinned_numbers_aug_2026", "test_unusual_rule",
    "test_aug_2026_is_a_knife_edge_do_not_build_a_case_on_it", "test_base_snapshot_has_no_driver",
    "test_net_revenue_on_base_has_one_noise_driver", "test_trap_item_month_differs_from_order_month",
}
STRUCTURAL_TESTS = sorted(n for n, f in vars(T).items()
                          if n.startswith("test_") and callable(f) and n not in PINNED_TESTS)


# ---------------------------------------------------------------- small helpers

def q(con: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql(sql, con, params=params)


def _ids(xs) -> str:
    """Integers from our own queries, so string-building the IN list is safe."""
    return ",".join(str(int(x)) for x in xs)


def _py(x):
    """numpy scalars cannot be bound as sqlite parameters; NaN means NULL."""
    if isinstance(x, np.generic):
        x = x.item()
    return None if isinstance(x, float) and math.isnan(x) else x


def _parse(s) -> datetime | None:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return None
    return datetime.strptime(s, TS)


def _fmt(t: datetime | None) -> str | None:
    return None if t is None else t.strftime(TS)


def _days(rng: np.random.Generator, lo: float, hi: float) -> timedelta:
    return timedelta(seconds=float(rng.uniform(lo * 86400, hi * 86400)))


def _fulfilment_times(rng, status, created, shipped=None, delivered=None):
    """Fill the missing shipped/delivered/returned stamps the way the base data spaces
    them: shipped 0-3 days after creation, delivered 0-5 after shipping, returned 0-3
    after delivery (see eda: all three gaps are bounded uniforms)."""
    if status in SHIPPED and shipped is None:
        shipped = created + _days(rng, 0, 3)
    if status in DELIVERED and delivered is None:
        delivered = shipped + _days(rng, 0, 5)
    returned = delivered + _days(rng, 0, 3) if status == "Returned" else None
    return shipped, delivered, returned


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month + "-01", "%Y-%m-%d")
    end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, end


def _new_uuid(rng: np.random.Generator) -> str:
    return str(uuid.UUID(bytes=rng.bytes(16), version=4))


# ---------------------------------------------------------------- primitives (each cascades)

USER_COLS = ("id", "first_name", "last_name", "email", "age", "gender", "state", "street_address", "postal_code",
             "city", "country", "latitude", "longitude", "traffic_source", "created_at")


def clone_orders(con, rng, order_ids, n_new: int, month: str, new_users: bool = False) -> dict:
    """Add `n_new` orders in `month` that look like `order_ids`: the same status mix, and
    each new order takes its item set from a DIFFERENT source order of the same gender
    (department follows gender in theLook), so nothing is an exact copy.

    new_users=False: the source order's user orders again (a repeat purchase: every
    clone is a "returning" order under R13, which tilts customer_type as well).
    new_users=True: each clone belongs to a freshly signed-up user copied from the
    source user (same acquisition source, country, ...; emails already collide in
    the base, R19) whose first order this is, so customer_type keeps its mix.

    Cascades into users (if asked), order_items, inventory_items (one unsold row
    per item, as in the base) and sessions (one purchase session per item, R15)."""
    src_o = q(con, f"SELECT * FROM orders WHERE order_id IN ({_ids(order_ids)})")
    src_i = q(con, f"SELECT oi.*, ii.created_at AS inv_created_at FROM order_items oi "
                   f"JOIN inventory_items ii ON ii.id = oi.inventory_item_id "
                   f"WHERE oi.order_id IN ({_ids(order_ids)})")
    users = q(con, f"SELECT * FROM users WHERE id IN ({_ids(src_o.user_id.unique())})").set_index("id")
    products = q(con, "SELECT * FROM products").set_index("id")
    start, end = _month_bounds(month)
    sess_pool = q(con, "SELECT traffic_source, browser, n_events FROM sessions "
                       "WHERE purchased = 1 AND started_at >= ? AND started_at < ?", (_fmt(start), _fmt(end)))
    next_order = con.execute("SELECT MAX(order_id) FROM orders").fetchone()[0] + 1
    next_item = con.execute("SELECT MAX(id) FROM order_items").fetchone()[0] + 1
    next_inv = con.execute("SELECT MAX(id) FROM inventory_items").fetchone()[0] + 1
    next_user = con.execute("SELECT MAX(id) FROM users").fetchone()[0] + 1

    src_o = src_o.reset_index(drop=True)
    items_by_order = {oid: g for oid, g in src_i.groupby("order_id")}
    donors_by_gender = {g: d.index.to_numpy() for g, d in src_o.groupby("gender")}
    # cycle through the sources in a shuffled order so each is cloned about equally often
    seq = np.tile(rng.permutation(len(src_o)), math.ceil(n_new / len(src_o)))[:n_new]
    span = (end - start).total_seconds()

    orders, items, invs, sessions, new_user_rows = [], [], [], [], []
    revenue = net = 0.0
    for i, si in enumerate(seq):
        s = src_o.iloc[si]
        u = users.loc[s.user_id]
        donor = src_o.iloc[rng.choice(donors_by_gender[s.gender])]
        d_items = items_by_order[donor.order_id]
        created = start + timedelta(seconds=float(rng.uniform(0, span)))
        shipped, delivered, returned = _fulfilment_times(rng, s.status, created)
        oid = next_order + i
        uid = int(s.user_id)
        if new_users:                                     # signed up shortly before the first order
            uid = next_user + i
            row = u.to_dict() | {"id": uid, "created_at": _fmt(created - _days(rng, 0, 2))}
            new_user_rows.append(tuple(_py(row[c]) for c in USER_COLS))
        orders.append((oid, uid, s.status, s.gender, _fmt(created), _fmt(returned),
                       _fmt(shipped), _fmt(delivered), int(len(d_items))))
        d_created = _parse(donor.created_at)
        for _, it in d_items.iterrows():
            item_created = created + (_parse(it.created_at) - d_created)          # R2: keep the item/order offset
            inv_created = item_created - (_parse(it.created_at) - _parse(it.inv_created_at))
            p = products.loc[it.product_id]
            items.append((next_item, oid, uid, int(it.product_id), next_inv, s.status,
                          _fmt(item_created), _fmt(shipped), _fmt(delivered), _fmt(returned), float(it.sale_price)))
            invs.append((next_inv, int(it.product_id), _fmt(inv_created), None, float(p.cost), p.category, p["name"],
                         p.brand, float(p.retail_price), p.department, p.sku, int(p.distribution_center_id)))
            sp = sess_pool.iloc[int(rng.integers(len(sess_pool)))]
            lag = timedelta(minutes=float(rng.uniform(5, 15) if rng.random() < 0.75 else rng.uniform(15, 2880)))
            sessions.append((_new_uuid(rng), uid, sp.traffic_source, sp.browser,
                             u.state, _fmt(item_created - lag), int(sp.n_events), 1, 1, 1))
            revenue += float(it.sale_price)
            net += 0.0 if s.status in K_mod.LOST_STATUSES else float(it.sale_price)
            next_item += 1
            next_inv += 1

    if new_user_rows:
        con.executemany(f"INSERT INTO users ({', '.join(USER_COLS)}) VALUES ({','.join('?' * len(USER_COLS))})",
                        new_user_rows)
    con.executemany("INSERT INTO orders (order_id, user_id, status, gender, created_at, returned_at, shipped_at, "
                    "delivered_at, num_of_item) VALUES (?,?,?,?,?,?,?,?,?)", orders)
    con.executemany("INSERT INTO order_items (id, order_id, user_id, product_id, inventory_item_id, status, created_at, "
                    "shipped_at, delivered_at, returned_at, sale_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)", items)
    con.executemany("INSERT INTO inventory_items (id, product_id, created_at, sold_at, cost, product_category, "
                    "product_name, product_brand, product_retail_price, product_department, product_sku, "
                    "product_distribution_center_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", invs)
    con.executemany("INSERT INTO sessions (session_id, user_id, traffic_source, browser, state, started_at, n_events, "
                    "saw_product, added_to_cart, purchased) VALUES (?,?,?,?,?,?,?,?,?,?)", sessions)
    con.commit()
    return {"orders": len(orders), "items": len(items), "revenue": revenue, "net_revenue": net,
            "order_id_range": [orders[0][0], orders[-1][0]], "inventory_items": len(invs), "sessions": len(sessions)}


def flip_to_returned(con, rng, order_ids) -> dict:
    """Turn orders into Returned ones, filling shipped/delivered/returned stamps that
    are missing (a Processing order that gets returned was delivered in between).
    Items always carry their order's status and stamps in the base, so both tables
    move together. Sessions and inventory are untouched: the sale happened."""
    o = q(con, f"SELECT order_id, status, created_at, shipped_at, delivered_at FROM orders "
               f"WHERE order_id IN ({_ids(order_ids)})")
    was = {s: int(n) for s, n in o.status.value_counts().items()}
    rows = []
    for r in o.itertuples():
        shipped, delivered, returned = _fulfilment_times(rng, "Returned", _parse(r.created_at),
                                                         _parse(r.shipped_at), _parse(r.delivered_at))
        rows.append((_fmt(shipped), _fmt(delivered), _fmt(returned), int(r.order_id)))
    con.executemany("UPDATE orders SET status='Returned', shipped_at=?, delivered_at=?, returned_at=? "
                    "WHERE order_id=?", rows)
    con.executemany("UPDATE order_items SET status='Returned', shipped_at=?, delivered_at=?, returned_at=? "
                    "WHERE order_id=?", rows)
    con.commit()
    it = q(con, f"SELECT COUNT(*) n, SUM(sale_price) rev FROM order_items WHERE order_id IN ({_ids(order_ids)})").iloc[0]
    lost_before = sum(v for s, v in was.items() if s in K_mod.LOST_STATUSES)
    assert lost_before == 0, f"{lost_before} of the orders were already cancelled/returned"
    return {"orders": len(rows), "items": int(it.n), "revenue_moved_out_of_net": float(it.rev), "status_before": was}


def drop_orders(con, order_ids) -> dict:
    """Make orders never have happened. Cascades: order_items, and the purchase session
    that produced each item (matched by user and time: sessions carry no order id, so
    this is the nearest purchase session of that user within 3 days before / 1 day
    after the item, R15). Inventory rows stay: the item was never sold, and the base
    already stores every referenced inventory row with sold_at NULL."""
    it = q(con, f"SELECT id, user_id, created_at, sale_price, order_id FROM order_items "
               f"WHERE order_id IN ({_ids(order_ids)})")
    it["created_at"] = pd.to_datetime(it.created_at)
    s = q(con, f"SELECT session_id, user_id, started_at FROM sessions WHERE purchased = 1 "
               f"AND user_id IN ({_ids(it.user_id.unique())})")
    s["started_at"] = pd.to_datetime(s.started_at)
    matched: set[str] = set()
    for r in it.sort_values("created_at").itertuples():
        c = s[(s.user_id == r.user_id) & (s.started_at >= r.created_at - timedelta(days=3))
              & (s.started_at <= r.created_at + timedelta(days=1)) & ~s.session_id.isin(matched)]
        if len(c):
            matched.add(c.iloc[(c.started_at - r.created_at).abs().argsort().iloc[0]].session_id)
    con.executemany("DELETE FROM sessions WHERE session_id = ?", [(x,) for x in matched])
    con.execute(f"DELETE FROM order_items WHERE order_id IN ({_ids(order_ids)})")
    con.execute(f"DELETE FROM orders WHERE order_id IN ({_ids(order_ids)})")
    con.commit()
    return {"orders": len(set(order_ids)), "items": len(it), "revenue": float(it.sale_price.sum()),
            "sessions_removed": len(matched), "items_without_session": int(len(it) - len(matched))}


# ---------------------------------------------------------------- scenarios

def s01_facebook_surge(con, rng) -> list[dict]:
    month, source, multiplier = "2026-08", "Facebook", 3.0
    pool = q(con, "SELECT o.order_id FROM orders o JOIN users u ON u.id = o.user_id "
                  "WHERE u.traffic_source = ? AND o.created_at >= ? AND o.created_at < ?",
             (source, *map(_fmt, _month_bounds(month)))).order_id.to_numpy()
    # New users, not repeat orders: cloning repeat orders makes every clone "returning"
    # (R13) and tips customer_type over R11 as well (returning lift 1.6, new 0.45).
    res = clone_orders(con, rng, pool, round((multiplier - 1) * len(pool)), month, new_users=True)
    return [{
        "event": "August 2026 Facebook acquisition campaign: about 700 new customers signed up "
                 "through Facebook and placed a first order, so Facebook-acquired orders were 3x "
                 "July's. Nothing else changed",
        "expect": {"dimension": "acq_source", "segment": source, "measure": "revenue", "direction": "up",
                   "unusual": True},
        "visible_in": ["revenue", "orders", "contribution(acq_source)", "users.created_at in August"],
        "params": {"month": month, "acq_source": source, "multiplier": multiplier, "source_orders": int(len(pool)),
                   "new_users": True},
        "delta": {month: {"revenue": res["revenue"], "net_revenue": res["net_revenue"],
                          "orders": res["orders"], "items": res["items"]}},
        "rows": res,
    }]


def s02_chicago_returns(con, rng) -> list[dict]:
    # Ongoing since July, not August only: an August-only hit shrinks the Jul->Aug net
    # change from 62k to 42k, which inflates every other segment's share of change by
    # 1.5x and pushes seven organically-growing segments over R11 as well. With both
    # months hit the change-based tests stay quiet (Chicago moves with the total) and
    # the signal is the LEVEL of the returned share (R21): ~40% at Chicago in both
    # months against ~10% everywhere else and ~10% in every earlier month.
    months, dc, rate = ["2026-07", "2026-08"], "Chicago IL", 0.80
    delta, rows, cands = {}, {}, {}
    for month in months:
        cand = q(con, """SELECT o.order_id, SUM(dc.name = ?) AS from_dc, COUNT(*) AS n
                         FROM orders o JOIN order_items oi USING (order_id)
                         JOIN products p ON p.id = oi.product_id
                         JOIN distribution_centers dc ON dc.id = p.distribution_center_id
                         WHERE o.created_at >= ? AND o.created_at < ? AND o.status NOT IN ('Cancelled', 'Returned')
                         GROUP BY o.order_id HAVING from_dc = n""", (dc, *map(_fmt, _month_bounds(month))))
        pick = rng.choice(cand.order_id.to_numpy(), size=round(rate * len(cand)), replace=False)
        res = flip_to_returned(con, rng, pick)
        delta[month] = {"revenue": 0.0, "net_revenue": -res["revenue_moved_out_of_net"], "orders": 0, "items": 0}
        rows[month], cands[month] = res, int(len(cand))
    return [{
        "event": f"{dc} distribution centre has been shipping a mislabelled batch since July 2026: 80% of "
                 "the orders it fulfils come back. Gross revenue is untouched, net revenue and the returned "
                 "share show it, and only when split by distribution centre",
        "expect": {"dimension": "dist_center", "segment": dc, "measure": "returned_share", "direction": "up",
                   "months": months, "unusual": False},
        "visible_in": ["leakage", "leakage_by_dist_center", "net_revenue vs revenue"],
        "not_visible_in": ["revenue", "orders", "contribution(*, revenue)",
                           "contribution(*, net_revenue) for Jul->Aug: both months are hit"],
        "params": {"months": months, "dist_center": dc, "return_rate": rate, "candidate_orders": cands,
                   "scope": "orders whose items all ship from the centre and were not already cancelled/returned"},
        "delta": delta,
        "rows": rows,
    }]


def s03_no_driver(con, rng) -> list[dict]:
    return []


SCENARIOS = {
    "S01": ("single_dim", s01_facebook_surge),
    "S02": ("mechanism_chain", s02_chicago_returns),
    "S03": ("no_driver", s03_no_driver),
}


# ---------------------------------------------------------------- verification

def verify(sid: str, db: Path, planted: list[dict], base: KPI) -> dict:
    """Three checks, all of which must pass before the truth is frozen:
    structural tests from test_kpi, planted arithmetic against the base, and threshold margins."""
    k = KPI(db)
    out: dict = {"structural_tests": {}, "arithmetic": {}, "margins": {}}
    fails: list[str] = []

    T.K = k
    try:
        for name in STRUCTURAL_TESTS:
            try:
                getattr(T, name)()
                out["structural_tests"][name] = "pass"
            except Exception as e:                       # noqa: BLE001
                out["structural_tests"][name] = f"FAIL {e}"
                fails.append(f"{name}: {e}")
    finally:
        T.K = base

    # base + planted == scenario, month by month, for every core total
    expected: dict[str, dict] = {}
    for p in planted:
        for month, d in p["delta"].items():
            for col, v in d.items():
                expected.setdefault(month, {}).setdefault(col, 0)
                expected[month][col] += v
    bm, sm = base.monthly(), k.monthly()
    for month in bm.index:
        for col in ("revenue", "net_revenue", "orders", "items"):
            want = bm.loc[month, col] + expected.get(month, {}).get(col, 0)
            got = sm.loc[month, col]
            if not math.isclose(want, got, rel_tol=1e-9, abs_tol=0.01):
                fails.append(f"arithmetic {month} {col}: base {bm.loc[month, col]:.2f} + planted "
                             f"{expected.get(month, {}).get(col, 0):.2f} != scenario {got:.2f}")
    out["arithmetic"] = {m: {c: round(float(v), 2) for c, v in d.items()} for m, d in expected.items()} or "identical to base"

    last = k.last_complete_months(1)[0]
    prev = k.previous_period(last)[0]
    z = k.zscore(last)
    out["margins"]["zscore_last_month"] = round(z, 2)
    if not planted:
        for measure in K_mod.MEASURES:
            found = [(d["dimension"], d["segment"]) for d in k.find_drivers(prev, last, measure=measure)]
            want = [(d["dimension"], d["segment"]) for d in base.find_drivers(prev, last, measure=measure)]
            out["margins"][f"drivers_{measure}"] = found
            if found != want:
                fails.append(f"no-driver scenario: {measure} drivers {found} differ from base {want}")
    for p in planted:
        e = p["expect"]
        tag = f"{e['dimension']}={e['segment']} ({e['measure']})"
        if e["measure"] == "returned_share":            # R21 level signal, not an R11 change signal
            out["margins"][tag] = _verify_leakage(k, base, e, last, prev, fails, tag)
            continue
        drivers = k.find_drivers(prev, last, measure=e["measure"])
        hit = [d for d in drivers if d["dimension"] == e["dimension"] and d["segment"] == e["segment"]]
        if not hit:
            fails.append(f"{tag}: not flagged by find_drivers; flagged: "
                         f"{[(d['dimension'], d['segment']) for d in drivers]}")
            continue
        d = hit[0]
        m = {"share_of_change": round(d["share_of_change"], 4), "lift": round(d["lift"], 3),
             "rank": drivers.index(d) + 1, "others_flagged": [(x["dimension"], x["segment"])
                                                              for x in drivers if x is not d]}
        out["margins"][tag] = m
        sign = 1 if e["direction"] == "up" else -1
        if sign * d["delta"] <= 0:
            fails.append(f"{tag}: moved the wrong way (delta {d['delta']:.0f})")
        if abs(d["share_of_change"]) < MARGIN["share"]:
            fails.append(f"{tag}: share of change {d['share_of_change']:.3f} < margin {MARGIN['share']}")
        if abs(d["lift"] - 1) < MARGIN["lift_gap"]:
            fails.append(f"{tag}: |lift - 1| = {abs(d['lift'] - 1):.2f} < margin {MARGIN['lift_gap']}")
        if m["rank"] != 1:
            fails.append(f"{tag}: not the largest driver (rank {m['rank']})")
        if e["unusual"] and abs(z) < MARGIN["z"]:
            fails.append(f"{tag}: |z| = {abs(z):.2f} < margin {MARGIN['z']}; the month is not clearly unusual")
        if not e["unusual"] and k.is_unusual(last) != base.is_unusual(last):
            fails.append(f"{tag}: the scenario changed whether {last} is unusual (R10); it should not")
        if "not_visible_in" in p and "revenue" in p["not_visible_in"]:
            gross = [(x["dimension"], x["segment"]) for x in k.find_drivers(prev, last)]
            if (e["dimension"], e["segment"]) in gross:
                fails.append(f"{tag}: visible in gross revenue, but the scenario says it must not be")
            out["margins"][tag]["gross_drivers"] = gross

    out["ok"] = not fails
    out["failures"] = fails
    return out


def _verify_leakage(k: KPI, base: KPI, e: dict, last: str, prev: str, fails: list[str], tag: str) -> dict:
    """A returns plant must be loud in the LEVEL of the returned share (planted segment
    at least 3x every other segment and at least 30 points, in every planted month)
    and silent everywhere else: gross drivers, R10, and the net driver list must be
    the base's own (both months are hit, so the Jul->Aug change test stays quiet)."""
    m: dict = {"returned_share": {}}
    for month in e["months"]:
        lk = k.leakage(month, by=e["dimension"])["returned"]
        others = lk.drop(e["segment"])
        m["returned_share"][month] = {"planted": round(float(lk[e["segment"]]), 3),
                                      "others_max": round(float(others.max()), 3),
                                      "base": round(float(base.leakage(month, by=e["dimension"])["returned"][e["segment"]]), 3)}
        if lk[e["segment"]] < 0.30 or lk[e["segment"]] < 3 * others.max():
            fails.append(f"{tag} {month}: returned share {lk[e['segment']]:.2f} is not 3x the others' max "
                         f"{others.max():.2f} and 0.30")
    m["returned_share_history"] = {mon: round(float(k.leakage(mon)["returned"]), 3) for mon in k.months()[-8:]}
    for measure in K_mod.MEASURES:
        found = [(d["dimension"], d["segment"]) for d in k.find_drivers(prev, last, measure=measure)]
        want = [(d["dimension"], d["segment"]) for d in base.find_drivers(prev, last, measure=measure)]
        m[f"drivers_{measure}"] = found
        if found != want:
            fails.append(f"{tag}: {measure} drivers {found} differ from the base's {want}; the change tests "
                         "should stay quiet")
    if k.is_unusual(last) != base.is_unusual(last):
        fails.append(f"{tag}: the scenario changed whether {last} is unusual (R10); it should not")
    return m


# ---------------------------------------------------------------- driver

def make(sid: str, base: KPI) -> dict:
    tag, fn = SCENARIOS[sid]
    db = SCEN_DIR / f"{sid}.sqlite"
    print(f"\n== {sid} ({tag})")
    if db.exists():
        db.unlink()
    shutil.copyfile(BASE_DB, db)
    rng = np.random.default_rng([SEED, int(sid[1:])])
    con = sqlite3.connect(db)
    try:
        planted = fn(con, rng)
    finally:
        con.close()
    for p in planted:
        p["scenario"], p["tag"] = sid, tag
        print(f"   planted: {p['event']}")
        print(f"   rows:    {p['rows']}")
    if not planted:
        print("   planted: nothing (control)")

    print(f"   verifying ({len(STRUCTURAL_TESTS)} structural tests, arithmetic, margins)...")
    v = verify(sid, db, planted, base)
    for f in v["failures"]:
        print(f"   FAIL {f}")
    print(f"   margins: {json.dumps(v['margins'], default=str)}")
    if not v["ok"]:
        raise SystemExit(f"{sid}: verification failed; truth NOT written")

    truth = make_truth.build(db, planted)
    out = TRUTH_DIR / f"{sid}.json"
    out.write_text(json.dumps(truth, indent=2, ensure_ascii=False), encoding="utf-8")
    lm = truth["last_month"]
    print(f"   truth -> {out.relative_to(ROOT)}: revenue {lm['headline']['revenue']:,.0f}, z {lm['zscore']:+.2f}, "
          f"unusual={lm['unusual']}, drivers={[(d['dimension'], d['segment']) for d in lm['drivers']]}, "
          f"net_drivers={[(d['dimension'], d['segment']) for d in lm['net_drivers']]}")
    return {
        "tag": tag, "db": f"scenarios/{sid}.sqlite", "db_sha256": truth["_meta"]["db_sha256"],
        "truth": f"reference/truth/{sid}.json", "seed": [SEED, int(sid[1:])],
        "generated_at_utc": truth["_meta"]["generated_at_utc"],
        "event": [p["event"] for p in planted] or ["control: the untouched snapshot"],
        "expect": [p["expect"] for p in planted],
        "verification": {"structural_tests_passed": sum(s == "pass" for s in v["structural_tests"].values()),
                         "structural_tests_run": len(v["structural_tests"]),
                         "arithmetic": v["arithmetic"], "margins": v["margins"]},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="scenario ids to (re)generate; default: all")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for sid, (tag, fn) in SCENARIOS.items():
            print(f"{sid}  {tag:<16} {fn.__name__}")
        return
    ids = a.ids or list(SCENARIOS)
    unknown = [i for i in ids if i not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario {unknown}; known: {list(SCENARIOS)}")

    SCEN_DIR.mkdir(exist_ok=True)
    TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    manifest.setdefault("_meta", {})["base_db_sha256"] = hashlib.sha256(BASE_DB.read_bytes()).hexdigest()
    base = T.K                                   # the base referee test_kpi already built
    for sid in ids:
        try:
            manifest[sid] = make(sid, base)
        except SystemExit:
            raise
        except Exception:                        # noqa: BLE001
            traceback.print_exc()
            raise SystemExit(f"{sid}: generation failed")
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
