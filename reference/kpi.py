"""reference/kpi.py -- the single source of truth for every KPI in this project.

Each function implements one rule from KPI_DEFINITIONS.md (the rule id is in the
docstring). Three things depend on this file:

    make_truth.py      computes the expected answer of every eval case from it
    the grader         recomputes the agent's numbers with it
    the script baseline walks the metric tree by calling it

It is NOT the agent and it is NOT the baseline. It is the referee. Truth never
comes from agent output (Week 3, the contamination trap).

This is the PANDAS path. queries/*.sql is an independent SQL path for the same
KPIs, and test_kpi.py asserts that the two agree.

    from reference.kpi import KPI
    k = KPI()                                  # data/thelook.sqlite, today = 2026-09-01
    k.revenue("2026-08")                       # 519369.9
    k.decompose("2026-07", "2026-08")
    k.contribution("category", "2026-07", "2026-08")
    k.find_drivers(["2026-03","2026-04","2026-05"], k.last_complete_months(3))
"""
from __future__ import annotations

import sqlite3
from functools import cached_property
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "thelook.sqlite"

TODAY = "2026-09-01"                     # R1
LOST_STATUSES = ("Cancelled", "Returned")  # R4
ZSCORE_LOOKBACK = 24                     # R10
UNUSUAL_Z = 2.0                          # R10
DRIVER_MIN_SHARE = 0.15                  # R11
DRIVER_MIN_LIFT_GAP = 0.5                # R11
MIN_TOTAL_CHANGE = 0.02                  # R11
UNKNOWN = "(unknown)"                    # R17

DIMENSIONS = ("customer_type", "acq_source", "country", "department",
              "category", "brand", "dist_center")
SESSION_DIMENSIONS = ("traffic_source", "browser")
MEASURES = {"revenue": "sale_price", "net_revenue": "net_sale_price"}   # R3 / R4, fact column per measure

_FACT_SQL = """
SELECT oi.id AS item_id, oi.order_id, o.user_id, oi.sale_price,
       substr(o.created_at, 1, 7) AS month,         -- R2: ORDER month, never item month
       o.created_at AS order_created_at, o.status AS order_status,
       p.cost, p.category, p.brand, p.department,
       dc.name AS dist_center,
       u.traffic_source AS acq_source,              -- R14: acquisition source
       u.country
FROM order_items oi
JOIN orders   o  USING (order_id)
JOIN products p  ON p.id  = oi.product_id
JOIN users    u  ON u.id  = o.user_id
JOIN distribution_centers dc ON dc.id = p.distribution_center_id
"""


def connect(db_path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Read-only connection. Writes are refused by SQLite itself, not by a prompt."""
    p = Path(db_path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"{p} not found. Run buildsql.py to build it from data/snapshot/.")
    return sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)


def _months(x) -> list[str]:
    return [x] if isinstance(x, str) else list(x)


class KPI:
    def __init__(self, db_path: str | Path = DEFAULT_DB, today: str = TODAY):
        self.db_path = Path(db_path)
        self.today = today
        self.con = connect(db_path)

    # ------------------------------------------------------------------ data

    @cached_property
    def fact(self) -> pd.DataFrame:
        """One row per item sold, every dimension attached."""
        f = pd.read_sql(_FACT_SQL, self.con)
        # R17: a missing dimension value is a segment, not a row to lose. pandas groupby
        # drops NaN keys silently; 24 products have no brand, and without this line the
        # brand segments stop adding up to total revenue (test_segments_sum_to_total).
        for d in ("category", "brand", "department", "dist_center", "acq_source", "country"):
            f[d] = f[d].fillna(UNKNOWN)
        first = f.groupby("user_id")["order_created_at"].transform("min")
        f["customer_type"] = (f["order_created_at"] == first).map({True: "new", False: "returning"})  # R13
        f["profit"] = f["sale_price"] - f["cost"]                                                     # R7
        f["lost"] = f["order_status"].isin(LOST_STATUSES)                                             # R4
        f["net_sale_price"] = f["sale_price"].where(~f["lost"], 0.0)                                  # R4, per item
        return f

    @cached_property
    def sessions(self) -> pd.DataFrame:
        return pd.read_sql("""SELECT substr(started_at, 1, 7) AS month, traffic_source, browser,
                                     added_to_cart, purchased FROM sessions""", self.con)

    # ------------------------------------------------------------------ time

    def months(self) -> list[str]:
        """Every COMPLETE month in the data: strictly before today's month (R1, R8)."""
        return list(self.monthly().index)

    def last_complete_months(self, n: int = 1) -> list[str]:
        """R8. 'Last 3 months' with today = 2026-09-01 is ['2026-06', '2026-07', '2026-08']."""
        return self.months()[-n:]

    def previous_period(self, months) -> list[str]:
        """R9. The period of equal length immediately before `months`."""
        ms, allm = _months(months), self.months()
        i = allm.index(ms[0])
        if i < len(ms):
            raise ValueError(f"no complete period of {len(ms)} months before {ms[0]}")
        return allm[i - len(ms): i]

    def _check(self, months) -> list[str]:
        ms, known = _months(months), set(self.months())
        bad = [m for m in ms if m not in known]
        if bad:                                     # loud, and says what IS valid (Week 2 p.32)
            allm = self.months()
            raise ValueError(f"no complete month {bad}; data covers {allm[0]} to {allm[-1]} "
                             f"(today is {self.today}, so {self.today[:7]} is not complete)")
        return ms

    # ------------------------------------------------------------------ core KPIs

    @cached_property
    def _monthly(self) -> pd.DataFrame:
        f = self.fact
        g = f.groupby("month")
        m = pd.DataFrame({
            "revenue": g["sale_price"].sum(),                                    # R3
            "net_revenue": f[~f["lost"]].groupby("month")["sale_price"].sum(),   # R4
            "orders": g["order_id"].nunique(),                                   # R5
            "items": g["item_id"].count(),                                       # R5
            "profit": g["profit"].sum(),                                         # R7
        }).fillna({"net_revenue": 0.0})
        m = m[m.index < self.today[:7]].sort_index()                             # complete months only
        m["aov"] = m["revenue"] / m["orders"]                                    # R6
        m["items_per_order"] = m["items"] / m["orders"]
        m["margin"] = m["profit"] / m["revenue"]                                 # R7
        m["mom"] = m["revenue"].pct_change()                                     # R9
        return m

    def monthly(self) -> pd.DataFrame:
        return self._monthly.copy()

    def _value(self, col: str, month: str) -> float:
        return float(self._monthly.loc[self._check(month)[0], col])

    def revenue(self, month: str) -> float:      return self._value("revenue", month)       # R3
    def net_revenue(self, month: str) -> float:  return self._value("net_revenue", month)   # R4
    def orders(self, month: str) -> int:         return int(self._value("orders", month))   # R5
    def items(self, month: str) -> int:          return int(self._value("items", month))    # R5
    def aov(self, month: str) -> float:          return self._value("aov", month)           # R6
    def profit(self, month: str) -> float:       return self._value("profit", month)        # R7
    def margin(self, month: str) -> float:       return self._value("margin", month)        # R7
    def mom(self, month: str) -> float:          return self._value("mom", month)           # R9

    def period(self, months) -> dict:
        """Totals over one or more months. AOV is total revenue / total orders (R6)."""
        t = self._monthly.loc[self._check(months), ["revenue", "net_revenue", "orders", "items", "profit"]].sum()
        return {"months": _months(months), "revenue": float(t["revenue"]), "net_revenue": float(t["net_revenue"]),
                "orders": int(t["orders"]), "items": int(t["items"]), "profit": float(t["profit"]),
                "aov": float(t["revenue"] / t["orders"]), "margin": float(t["profit"] / t["revenue"])}

    # ------------------------------------------------------------------ explaining a change

    def decompose(self, prev, cur) -> dict:
        """R12. Volume effect + AOV effect == revenue change, exactly."""
        a, b = self.period(prev), self.period(cur)
        delta = b["revenue"] - a["revenue"]
        vol = (b["orders"] - a["orders"]) * a["aov"]
        aov = b["orders"] * (b["aov"] - a["aov"])
        return {"prev": a["months"], "cur": b["months"],
                "revenue_prev": a["revenue"], "revenue_cur": b["revenue"],
                "delta": delta, "delta_pct": delta / a["revenue"],
                "volume_effect": vol, "aov_effect": aov,
                "volume_share": vol / delta if delta else float("nan"),
                "aov_share": aov / delta if delta else float("nan")}

    def zscore(self, month: str, before: str | None = None, lookback: int = ZSCORE_LOOKBACK) -> float:
        """R10. z of `month`'s MoM growth against the `lookback` months before `before`.

        `before` defaults to `month` itself. For a multi-month window pass the
        window's FIRST month, so every month in the window is judged against the
        same baseline and the window does not contaminate its own yardstick.
        """
        self._check(month)
        mom = self._monthly["mom"]
        i = list(mom.index).index(before or month)
        hist = mom.iloc[max(i - lookback, 1): i]          # skip the very first month (mom is NaN)
        if len(hist) < 6:
            raise ValueError(f"only {len(hist)} months of history before {before or month}; need at least 6")
        return float((mom.loc[month] - hist.mean()) / hist.std())

    def is_unusual(self, month: str, before: str | None = None) -> bool:
        return abs(self.zscore(month, before)) >= UNUSUAL_Z

    @staticmethod
    def _measure_col(measure: str) -> str:
        if measure not in MEASURES:
            raise ValueError(f"unknown measure {measure!r}; available: {', '.join(MEASURES)}")
        return MEASURES[measure]

    def contribution(self, dim: str, prev, cur, measure: str = "revenue") -> pd.DataFrame:
        """Revenue by segment in both periods, and each segment's share of the change.

        lift = share_of_change / share_of_base. 1.0 = moved with everything else.
        `measure` is "revenue" (R3, the default) or "net_revenue" (R4).
        """
        if dim not in DIMENSIONS:
            raise ValueError(f"unknown dimension {dim!r}; available: {', '.join(DIMENSIONS)}")
        p, c, f, col = self._check(prev), self._check(cur), self.fact, self._measure_col(measure)
        g = pd.DataFrame({"prev": f[f["month"].isin(p)].groupby(dim)[col].sum(),
                          "cur": f[f["month"].isin(c)].groupby(dim)[col].sum()}).fillna(0.0)
        g["delta"] = g["cur"] - g["prev"]
        g["share_of_change"] = g["delta"] / g["delta"].sum()
        g["share_of_base"] = g["prev"] / g["prev"].sum()
        g["lift"] = g["share_of_change"] / g["share_of_base"]
        return g.reindex(g["delta"].abs().sort_values(ascending=False).index)

    def find_drivers(self, prev, cur, dims=DIMENSIONS, measure: str = "revenue") -> list[dict]:
        """R11. Segments with a large share of the change AND lift far from 1.

        Empty list = no single driver. That is a valid and common answer.
        With measure="net_revenue" the same rule is applied to R4 net revenue, which
        is how a returns or cancellation problem shows up (gross does not move).
        """
        self._measure_col(measure)
        a, b = self.period(prev)[measure], self.period(cur)[measure]
        if abs(b - a) / a < MIN_TOTAL_CHANGE:
            return []
        out = []
        for dim in dims:
            g = self.contribution(dim, prev, cur, measure)
            hit = g[(g["share_of_change"].abs() >= DRIVER_MIN_SHARE)
                    & ((g["lift"] - 1).abs() >= DRIVER_MIN_LIFT_GAP)]
            for seg, r in hit.iterrows():
                out.append({"dimension": dim, "segment": seg, "measure": measure, "delta": float(r["delta"]),
                            "share_of_change": float(r["share_of_change"]),
                            "share_of_base": float(r["share_of_base"]), "lift": float(r["lift"])})
        return sorted(out, key=lambda x: -abs(x["share_of_change"]))

    # ------------------------------------------------------------------ funnel and leakage

    def funnel(self, month: str, by: str | None = None) -> pd.DataFrame:
        """R15. Sessions, cart rate, purchase rate for ONE month.

        Compare across `by` within the month. Never compare these rates across
        months: the purchase rate rises mechanically over time in this dataset.
        """
        if by is not None and by not in SESSION_DIMENSIONS:
            raise ValueError(f"unknown session dimension {by!r}; available: {', '.join(SESSION_DIMENSIONS)}")
        s = self.sessions[self.sessions["month"] == self._check(month)[0]]
        key = by if by else (lambda _: "all")
        g = s.groupby(key).agg(sessions=("purchased", "size"), cart_rate=("added_to_cart", "mean"),
                               purchase_rate=("purchased", "mean"))
        return g.sort_values("sessions", ascending=False)

    def leakage(self, month: str, by: str | None = None):
        """Share of ORDERS (not items) cancelled and returned.

        With `by`, one row per segment of that dimension: the share of the segment's
        gross REVENUE that sits in cancelled / returned orders (R21). Revenue-weighted
        because an order can span segments, so orders cannot be split cleanly.
        """
        f = self.fact[self.fact["month"] == self._check(month)[0]]
        if by is None:
            share = f.drop_duplicates("order_id")["order_status"].value_counts(normalize=True)
            return {"cancelled": float(share.get("Cancelled", 0.0)), "returned": float(share.get("Returned", 0.0))}
        if by not in DIMENSIONS:
            raise ValueError(f"unknown dimension {by!r}; available: {', '.join(DIMENSIONS)}")
        g = pd.DataFrame({"revenue": f.groupby(by)["sale_price"].sum(),
                          "cancelled": f[f["order_status"] == "Cancelled"].groupby(by)["sale_price"].sum(),
                          "returned": f[f["order_status"] == "Returned"].groupby(by)["sale_price"].sum()}).fillna(0.0)
        g["cancelled"] = g["cancelled"] / g["revenue"]
        g["returned"] = g["returned"] / g["revenue"]
        return g.sort_values("revenue", ascending=False)
