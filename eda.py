"""eda.py -- what is in this dataset, and can we trust it?

    & "C:\\Users\\Ting\\anaconda3\\python.exe" eda.py

Reads data/thelook.sqlite read-only. Prints: what the business is, the tables
and how they link, products and brands, customers, orders and their lifecycle,
and a data-quality audit (nulls, keys, referential integrity, timestamp order,
status consistency, value ranges, edge months).
"""
import sqlite3
import sys

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 180)
pd.set_option("display.max_columns", 30)
pd.set_option("display.float_format", lambda v: f"{v:,.2f}")

con = sqlite3.connect("file:data/thelook.sqlite?mode=ro", uri=True)
q = lambda sql, **p: pd.read_sql(sql, con, params=p or None)
one = lambda sql, *p: con.execute(sql, p).fetchone()


def h(t):
    print(f"\n{'=' * 90}\n{t}\n{'=' * 90}")


# ---------------------------------------------------------------- 1. tables and links
h("1. TABLES, ROWS, KEYS")
for t, pk in [("users", "id"), ("orders", "order_id"), ("order_items", "id"), ("products", "id"),
              ("inventory_items", "id"), ("sessions", "session_id"), ("distribution_centers", "id")]:
    n, nk = one(f'SELECT COUNT(*), COUNT(DISTINCT "{pk}") FROM "{t}"')
    print(f"  {t:<22}{n:>10,} rows   pk {pk:<11} unique: {'yes' if n == nk else 'NO'}")

print("\n  referential integrity (rows whose foreign key points at nothing):")
for label, sql in [
    ("order_items.order_id -> orders", "SELECT COUNT(*) FROM order_items WHERE order_id NOT IN (SELECT order_id FROM orders)"),
    ("order_items.product_id -> products", "SELECT COUNT(*) FROM order_items WHERE product_id NOT IN (SELECT id FROM products)"),
    ("order_items.inventory_item_id -> inventory_items", "SELECT COUNT(*) FROM order_items WHERE inventory_item_id NOT IN (SELECT id FROM inventory_items)"),
    ("orders.user_id -> users", "SELECT COUNT(*) FROM orders WHERE user_id NOT IN (SELECT id FROM users)"),
    ("inventory_items.product_id -> products", "SELECT COUNT(*) FROM inventory_items WHERE product_id NOT IN (SELECT id FROM products)"),
    ("products.distribution_center_id -> distribution_centers", "SELECT COUNT(*) FROM products WHERE distribution_center_id NOT IN (SELECT id FROM distribution_centers)"),
    ("sessions.user_id -> users (non-null only)", "SELECT COUNT(*) FROM sessions WHERE user_id IS NOT NULL AND user_id NOT IN (SELECT id FROM users)"),
    ("orders with zero items", "SELECT COUNT(*) FROM orders WHERE order_id NOT IN (SELECT order_id FROM order_items)"),
    ("users with zero orders", "SELECT COUNT(*) FROM users WHERE id NOT IN (SELECT user_id FROM orders)"),
]:
    print(f"    {label:<58}{one(sql)[0]:>8,}")

# ---------------------------------------------------------------- 2. nulls
h("2. NULLS PER COLUMN (only columns that have any)")
for t in ["users", "orders", "order_items", "products", "inventory_items", "sessions", "distribution_centers"]:
    cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
    n = one(f"SELECT COUNT(*) FROM {t}")[0]
    nulls = {c: one(f'SELECT COUNT(*) FROM {t} WHERE "{c}" IS NULL')[0] for c in cols}
    nulls = {c: v for c, v in nulls.items() if v}
    if nulls:
        print(f"  {t}: " + ", ".join(f"{c}={v:,} ({100 * v / n:.1f}%)" for c, v in nulls.items()))
    else:
        print(f"  {t}: none")

# ---------------------------------------------------------------- 3. the business
h("3. WHAT BUSINESS IS THIS")
print("  departments:", one("SELECT GROUP_CONCAT(department || ':' || n, '  ') FROM (SELECT department, COUNT(*) n FROM products GROUP BY 1)")[0])
print("  categories :", one("SELECT COUNT(DISTINCT category) FROM products")[0],
      "| brands:", one("SELECT COUNT(DISTINCT brand) FROM products")[0],
      "| products:", one("SELECT COUNT(*) FROM products")[0],
      "| distribution centres:", one("SELECT COUNT(*) FROM distribution_centers")[0], "(all US)")
print("  sellers table: none -> a single retailer, not a marketplace")
print(q("SELECT name AS distribution_center, ROUND(latitude,1) lat, ROUND(longitude,1) lon FROM distribution_centers").to_string(index=False))

# ---------------------------------------------------------------- 4. products and brands
h("4. PRODUCTS AND BRANDS  (revenue = all-time gross, by order month rule)")
rev = q("""SELECT p.category, p.department, p.brand, oi.sale_price, p.retail_price, p.cost
           FROM order_items oi JOIN products p ON p.id = oi.product_id""")
tot = rev["sale_price"].sum()
cat = rev.groupby("category")["sale_price"].agg(["sum", "count"]).sort_values("sum", ascending=False)
cat["share_%"] = 100 * cat["sum"] / tot
cat["avg_price"] = cat["sum"] / cat["count"]
print("  top 10 categories by revenue\n" + cat.head(10).rename(columns={"sum": "revenue", "count": "items"}).to_string())
print(f"\n  department split: " + ", ".join(f"{d} {100 * v / tot:.1f}%" for d, v in rev.groupby("department")["sale_price"].sum().items()))
br = rev.groupby("brand")["sale_price"].sum().sort_values(ascending=False)
cum = br.cumsum() / tot
print(f"\n  top 10 brands by revenue (share): " + ", ".join(f"{b} {100 * v / tot:.1f}%" for b, v in br.head(10).items()))
print(f"  brands needed for 50% of revenue: {int((cum < 0.5).sum()) + 1} of {len(br):,}   |   for 80%: {int((cum < 0.8).sum()) + 1}")
print(f"  brands with a single sale ever: {int((rev.groupby('brand').size() == 1).sum()):,}")
print("\n  price sanity")
print(f"    sale_price == retail_price on {100 * (rev['sale_price'].round(2) == rev['retail_price'].round(2)).mean():.1f}% of items  "
      f"(so 'discount' is not a lever in this data)")
print(f"    sale_price <= 0: {int((rev['sale_price'] <= 0).sum())}   cost <= 0: {int((rev['cost'] <= 0).sum())}   "
      f"cost > sale_price (negative margin): {int((rev['cost'] > rev['sale_price']).sum())}")
print(f"    sale_price percentiles: " + ", ".join(f"p{p}={v:,.0f}" for p, v in rev['sale_price'].quantile([.05, .25, .5, .75, .95, .99]).rename(lambda x: int(x * 100)).items()))
print(f"    margin (1 - cost/price): min {100 * (1 - rev['cost'] / rev['sale_price']).min():.0f}%  median {100 * (1 - rev['cost'] / rev['sale_price']).median():.0f}%  max {100 * (1 - rev['cost'] / rev['sale_price']).max():.0f}%")
print(f"    products never sold: {one('SELECT COUNT(*) FROM products WHERE id NOT IN (SELECT product_id FROM order_items)')[0]:,} of {len(rev.groupby('category')) and one('SELECT COUNT(*) FROM products')[0]:,}")

# ---------------------------------------------------------------- 5. customers
h("5. CUSTOMERS")
u = q("SELECT id, age, gender, country, traffic_source, substr(created_at,1,4) yr FROM users")
print("  countries (share of users): " + ", ".join(f"{c} {100 * v / len(u):.1f}%" for c, v in u["country"].value_counts().head(8).items()))
print("  gender: " + ", ".join(f"{g} {100 * v / len(u):.1f}%" for g, v in u["gender"].value_counts().items())
      + f"   | age: min {u['age'].min()}, median {u['age'].median():.0f}, max {u['age'].max()}")
print("  acquisition source: " + ", ".join(f"{s} {100 * v / len(u):.1f}%" for s, v in u["traffic_source"].value_counts().items()))
print("  users created per year: " + ", ".join(f"{y}:{v:,}" for y, v in u["yr"].value_counts().sort_index().items()))
opu = q("SELECT user_id, COUNT(*) n FROM orders GROUP BY 1")["n"]
print(f"  orders per buyer: 1 order {100 * (opu == 1).mean():.1f}%, 2 orders {100 * (opu == 2).mean():.1f}%, 3+ {100 * (opu >= 3).mean():.1f}%   (max {opu.max()})")
print(f"  orders placed BEFORE the user's created_at: "
      f"{one('SELECT COUNT(*) FROM orders o JOIN users u ON u.id=o.user_id WHERE o.created_at < u.created_at')[0]:,}")
print(f"  duplicate emails: {one('SELECT COUNT(*) - COUNT(DISTINCT email) FROM users')[0]:,}")

# ---------------------------------------------------------------- 6. orders and lifecycle
h("6. ORDERS AND THEIR LIFECYCLE")
o = q("SELECT order_id, user_id, status, created_at, shipped_at, delivered_at, returned_at, num_of_item FROM orders",)
for c in ("created_at", "shipped_at", "delivered_at", "returned_at"):
    o[c] = pd.to_datetime(o[c])
print(f"  span: {o['created_at'].min():%Y-%m-%d} to {o['created_at'].max():%Y-%m-%d}")
yr = o.groupby(o["created_at"].dt.year).size()
print("  orders per year: " + ", ".join(f"{y}:{v:,}" for y, v in yr.items()))
print("  status: " + ", ".join(f"{s} {100 * v / len(o):.1f}%" for s, v in o["status"].value_counts().items()))
items_per = q("SELECT order_id, COUNT(*) n FROM order_items GROUP BY 1").set_index("order_id")["n"]
print(f"  num_of_item matches actual item count: {100 * (o.set_index('order_id')['num_of_item'] == items_per).mean():.1f}%"
      f"   | items per order: mean {items_per.mean():.2f}, max {items_per.max()}")

print("\n  status vs timestamps (should be consistent; counts are violations)")
chk = {
    "Shipped/Complete/Returned without shipped_at": ((o["status"].isin(["Shipped", "Complete", "Returned"])) & o["shipped_at"].isna()).sum(),
    "Complete/Returned without delivered_at": ((o["status"].isin(["Complete", "Returned"])) & o["delivered_at"].isna()).sum(),
    "Returned without returned_at": ((o["status"] == "Returned") & o["returned_at"].isna()).sum(),
    "Processing/Cancelled WITH shipped_at": ((o["status"].isin(["Processing", "Cancelled"])) & o["shipped_at"].notna()).sum(),
    "Shipped WITH delivered_at": ((o["status"] == "Shipped") & o["delivered_at"].notna()).sum(),
    "not Returned but has returned_at": ((o["status"] != "Returned") & o["returned_at"].notna()).sum(),
}
for k, v in chk.items():
    print(f"    {k:<48}{v:>8,}")

print("\n  timestamp order (violations) and typical gaps")
ship, deliv, ret = o["shipped_at"] - o["created_at"], o["delivered_at"] - o["shipped_at"], o["returned_at"] - o["delivered_at"]
for name, gap in (("created -> shipped", ship), ("shipped -> delivered", deliv), ("delivered -> returned", ret)):
    g = gap.dropna()
    print(f"    {name:<24}negative: {int((g < pd.Timedelta(0)).sum()):>6,}   "
          f"median {g.median().total_seconds() / 86400:5.1f} d   p95 {g.quantile(.95).total_seconds() / 86400:5.1f} d   max {g.max().total_seconds() / 86400:6.1f} d")
print(f"    order timestamps after the 2026-09-01 cutoff: shipped {int((o['shipped_at'] >= '2026-09-01').sum()):,}, "
      f"delivered {int((o['delivered_at'] >= '2026-09-01').sum()):,}, returned {int((o['returned_at'] >= '2026-09-01').sum()):,}"
      f"   (status reflects the export day, not the cutoff)")

oi = q("SELECT oi.order_id, oi.status item_status, o.status order_status, oi.created_at ic, o.created_at oc, oi.shipped_at ish, o.shipped_at osh "
       "FROM order_items oi JOIN orders o USING(order_id)")
print(f"\n  item status == order status: {100 * (oi['item_status'] == oi['order_status']).mean():.1f}%")
print(f"  item created_at == order created_at: {100 * (oi['ic'] == oi['oc']).mean():.1f}%   "
      f"(item shipped_at == order shipped_at: {100 * (oi['ish'].fillna('x') == oi['osh'].fillna('x')).mean():.1f}%)")

# ---------------------------------------------------------------- 7. inventory and sessions
h("7. INVENTORY AND SESSIONS")
print(f"  inventory items: {one('SELECT COUNT(*) FROM inventory_items')[0]:,}   sold (sold_at not null): {one('SELECT COUNT(*) FROM inventory_items WHERE sold_at IS NOT NULL')[0]:,}   "
      f"referenced by an order item: {one('SELECT COUNT(DISTINCT inventory_item_id) FROM order_items')[0]:,}")
print(f"  inventory cost == product cost: {100 * one('SELECT AVG(ROUND(i.cost,4) = ROUND(p.cost,4)) FROM inventory_items i JOIN products p ON p.id=i.product_id')[0]:.1f}%")
s = q("SELECT substr(started_at,1,4) yr, user_id IS NULL anon, purchased, added_to_cart, n_events FROM sessions")
print(f"  sessions: {len(s):,}   anonymous: {100 * s['anon'].mean():.1f}%   purchased: {100 * s['purchased'].mean():.1f}%   "
      f"events per session: median {s['n_events'].median():.0f}, max {s['n_events'].max()}")
print("  purchase rate by year: " + ", ".join(f"{y} {100 * v:.0f}%" for y, v in s.groupby("yr")["purchased"].mean().items()))
print(f"  non-purchase sessions per year: " + ", ".join(f"{y}:{v:,}" for y, v in s[s['purchased'] == 0].groupby('yr').size().items()))

# ---------------------------------------------------------------- 8. edge months
h("8. EDGE MONTHS  (is the first month partial? is the last month complete?)")
m = q("""SELECT substr(o.created_at,1,7) month, COUNT(DISTINCT o.order_id) orders, ROUND(SUM(oi.sale_price)) revenue
         FROM orders o JOIN order_items oi USING(order_id) GROUP BY 1 ORDER BY 1""")
print("  first 3 months:\n" + m.head(3).to_string(index=False))
print("  last 3 months:\n" + m.tail(3).to_string(index=False))
d = q("SELECT substr(created_at,1,10) day, COUNT(*) n FROM orders WHERE created_at >= '2026-08-25' GROUP BY 1 ORDER BY 1")
print("  last week of Aug, orders per day: " + ", ".join(f"{r.day[-2:]}:{r.n}" for r in d.itertuples()))
