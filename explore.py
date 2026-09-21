"""explore.py -- walk the revenue metric tree on the frozen theLook snapshot.

    & "C:\\Users\\Ting\\anaconda3\\python.exe" explore.py
    & "C:\\Users\\Ting\\anaconda3\\python.exe" explore.py 2026-06 2026-05     # any month pair: CUR PREV

One question, asked the way the agent will have to answer it:
    "Why did revenue change in CUR compared with PREV?"

KPI DEFINITIONS USED HERE (write these into the KPI doc; the agent must follow them)
    revenue      SUM(order_items.sale_price), all statuses            (gross)
    net revenue  same, excluding Cancelled and Returned orders
    month        taken from ORDERS.created_at, never order_items.created_at
                 (the two disagree on ~3% of items; see the last section)
    profit       revenue - SUM(products.cost)
    new order    the user's first ever order; everything after is "returning"
"""
import sqlite3
import sys

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 160)
pd.set_option("display.float_format", lambda v: f"{v:,.1f}")

CUR = sys.argv[1] if len(sys.argv) > 1 else "2026-08"
PREV = sys.argv[2] if len(sys.argv) > 2 else "2026-07"

con = sqlite3.connect("file:data/thelook.sqlite?mode=ro", uri=True)     # read-only, same as the agent


def h(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------------------------------------------------------------------------
# One fact table: one row per item sold, with every dimension attached.
# ---------------------------------------------------------------------------
fact = pd.read_sql("""
    SELECT oi.id, oi.order_id, oi.user_id, oi.sale_price,
           substr(o.created_at, 1, 7)  AS month,
           o.created_at                AS order_created_at,
           o.status                    AS order_status,
           p.cost, p.category, p.brand, p.department,
           dc.name                     AS dist_center,
           u.traffic_source            AS acq_source,
           u.country
    FROM order_items oi
    JOIN orders   o  USING (order_id)
    JOIN products p  ON p.id  = oi.product_id
    JOIN users    u  ON u.id  = oi.user_id
    JOIN distribution_centers dc ON dc.id = p.distribution_center_id
""", con)
first_order = fact.groupby("user_id")["order_created_at"].transform("min")
fact["customer_type"] = (fact["order_created_at"] == first_order).map({True: "new", False: "returning"})
fact["profit"] = fact["sale_price"] - fact["cost"]
fact["lost"] = fact["order_status"].isin(["Cancelled", "Returned"])

# ---------------------------------------------------------------------------
h("1. TREND  -- the top of the tree: revenue = orders x AOV")
# ---------------------------------------------------------------------------
m = fact.groupby("month").agg(revenue=("sale_price", "sum"), profit=("profit", "sum"),
                              orders=("order_id", "nunique"), items=("id", "count"))
m["net_revenue"] = fact[~fact["lost"]].groupby("month")["sale_price"].sum()
m["aov"] = m["revenue"] / m["orders"]
m["items_per_order"] = m["items"] / m["orders"]
m["margin_%"] = 100 * m["profit"] / m["revenue"]
m["mom_%"] = 100 * m["revenue"].pct_change()
print(m[["revenue", "mom_%", "net_revenue", "orders", "aov", "items_per_order", "margin_%"]].tail(8).to_string())

# ---------------------------------------------------------------------------
h(f"2. DECOMPOSITION  {PREV} -> {CUR}:  did volume or basket size move revenue?")
# ---------------------------------------------------------------------------
a, b = m.loc[PREV], m.loc[CUR]
d_rev = b["revenue"] - a["revenue"]
vol = (b["orders"] - a["orders"]) * a["aov"]
aov = b["orders"] * (b["aov"] - a["aov"])
print(f"  revenue        {a['revenue']:>12,.0f} -> {b['revenue']:>12,.0f}   delta {d_rev:>+11,.0f}")
print(f"  from orders    {a['orders']:>12,.0f} -> {b['orders']:>12,.0f}   explains {vol:>+11,.0f}  ({vol / d_rev:>5.0%})")
print(f"  from AOV       {a['aov']:>12,.2f} -> {b['aov']:>12,.2f}   explains {aov:>+11,.0f}  ({aov / d_rev:>5.0%})")

# ---------------------------------------------------------------------------
h("3. FUNNEL  -- orders = sessions x conversion.  Is it traffic, or conversion?")
# ---------------------------------------------------------------------------
s = pd.read_sql("""SELECT substr(started_at, 1, 7) AS month, traffic_source, browser,
                          saw_product, added_to_cart, purchased FROM sessions""", con)
f = s.groupby("month").agg(sessions=("purchased", "size"), saw_product=("saw_product", "mean"),
                           added_to_cart=("added_to_cart", "mean"), purchased=("purchased", "mean"))
f[["saw_product", "added_to_cart", "purchased"]] *= 100
print(f.tail(4).to_string())
for dim in ("traffic_source", "browser"):
    g = s[s["month"].isin([PREV, CUR])].groupby([dim, "month"]).agg(
        sessions=("purchased", "size"), conv=("purchased", "mean")).unstack("month")
    out = pd.DataFrame({"sessions_prev": g[("sessions", PREV)], "sessions_cur": g[("sessions", CUR)],
                        "conv%_prev": 100 * g[("conv", PREV)], "conv%_cur": 100 * g[("conv", CUR)]})
    out["sessions_chg_%"] = 100 * (out["sessions_cur"] / out["sessions_prev"] - 1)
    print(f"\n  by {dim}\n{out.sort_values('sessions_cur', ascending=False).to_string()}")


# ---------------------------------------------------------------------------
h(f"4. CONTRIBUTION  -- which segment carried the change?  ({PREV} -> {CUR})")
# ---------------------------------------------------------------------------
def contribution(dim, top=5):
    """Revenue by segment in both months, each segment's share of the total change.

    `lift` compares a segment's share of the CHANGE with its share of the BASE.
    1.0 means it simply grew with everything else. Far from 1.0 is a driver.
    This function is the seed of the scripted baseline the agent has to beat.
    """
    g = fact[fact["month"].isin([PREV, CUR])].pivot_table(index=dim, columns="month", values="sale_price",
                                                           aggfunc="sum", fill_value=0.0)
    g["delta"] = g[CUR] - g[PREV]
    g["share_of_change_%"] = 100 * g["delta"] / g["delta"].sum()
    g["share_of_base_%"] = 100 * g[PREV] / g[PREV].sum()
    g["lift"] = g["share_of_change_%"] / g["share_of_base_%"]
    g = g.reindex(g["delta"].abs().sort_values(ascending=False).index).head(top)
    print(f"\n  by {dim}\n{g.to_string()}")


for dim in ("customer_type", "acq_source", "department", "category", "country", "dist_center", "brand"):
    contribution(dim)

# ---------------------------------------------------------------------------
h("5. MARGIN  -- can revenue rise while profit falls?  Only if mix or cost moves.")
# ---------------------------------------------------------------------------
c = fact[fact["month"] == CUR].groupby("category").agg(revenue=("sale_price", "sum"), profit=("profit", "sum"))
c["margin_%"] = 100 * c["profit"] / c["revenue"]
c["rev_share_%"] = 100 * c["revenue"] / c["revenue"].sum()
c = c.sort_values("margin_%")
print("  lowest-margin categories\n" + c.head(4).to_string())
print("\n  highest-margin categories\n" + c.tail(4).to_string())

# ---------------------------------------------------------------------------
h("6. LEAKAGE  -- cancelled and returned orders, by month and by distribution centre")
# ---------------------------------------------------------------------------
orders = fact.drop_duplicates("order_id")
lk = orders.groupby("month")["order_status"].value_counts(normalize=True).unstack().mul(100)
print(lk[["Cancelled", "Returned"]].tail(4).to_string())
dc = fact[fact["month"].isin([PREV, CUR])].groupby("dist_center").agg(
    items=("id", "count"), returned=("order_status", lambda x: 100 * (x == "Returned").mean()))
print("\n  return rate % by distribution centre\n" + dc.sort_values("returned", ascending=False).to_string())

# ---------------------------------------------------------------------------
h("7. TRAP  -- the same KPI, two timestamps, two answers")
# ---------------------------------------------------------------------------
by_item = con.execute("SELECT SUM(sale_price) FROM order_items WHERE substr(created_at,1,7)=?", (CUR,)).fetchone()[0]
print(f"  {CUR} revenue by ORDER month : {m.loc[CUR, 'revenue']:>12,.0f}   <- the definition")
print(f"  {CUR} revenue by ITEM month  : {by_item:>12,.0f}   <- what a careless query returns")
