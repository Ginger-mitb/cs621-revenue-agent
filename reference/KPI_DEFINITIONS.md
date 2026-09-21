# KPI definitions (v1)

These rules are **stipulated by the team, not discovered in the data**. Several of
them have more than one reasonable alternative; we picked one and wrote it down.
This file is used three ways: the agent retrieves it, `reference/kpi.py`
implements it, and the grader judges against it. Change a rule here and you
must change `kpi.py`, re-run `test_kpi.py`, and regenerate the truth files.

Snapshot: `bigquery-public-data.thelook_ecommerce`, frozen with cutoff
2026-09-01 (see `data/snapshot/manifest.json`). The data is synthetic.

## Time

| Rule | Definition |
|---|---|
| **R1 Today** | The agent's "today" is **2026-09-01**. It must be told this; it cannot infer it. |
| **R2 Month attribution** | A sale belongs to the month of **`orders.created_at`**. Never use `order_items.created_at`: the two differ on almost every row and put ~3% of items in a different month. Aug 2026 revenue is 519,370 by order month and 516,172 by item month. Only the first is correct. |
| **R8 Default period** | No period given means the **last complete month (2026-08)**. "Last N months" means N complete calendar months ending 2026-08. "This month" (2026-09) has no data: say so. |
| **R9 Default comparison** | The **previous period of equal length** (month over month). Year over year only when asked, because this data grows so fast that YoY is always a large increase. |

## Money

| Rule | Definition |
|---|---|
| **R3 Revenue** | `SUM(order_items.sale_price)` over **all** order statuses (gross). This is what "revenue", "sales", "turnover" and "GMV" mean unless the user says otherwise. |
| **R4 Net revenue** | Revenue excluding orders whose status is `Cancelled` or `Returned`. Only when the user asks for "net". |
| **R5 Orders / items** | Orders = distinct orders with at least one item, all statuses. Items = rows of `order_items`. |
| **R6 AOV** | Revenue / orders. Over several months: total revenue / total orders, **not** the mean of monthly AOVs. |
| **R7 Profit / margin** | Profit = `SUM(sale_price - products.cost)`. Margin = profit / revenue. |

## Explaining a change

| Rule | Definition |
|---|---|
| **R12 Decomposition** | Volume effect = (orders_cur − orders_prev) × AOV_prev. AOV effect = orders_cur × (AOV_cur − AOV_prev). The two sum exactly to the revenue change. |
| **R10 Unusual** | A month is unusual when the z-score of its MoM growth, against the 24 months before the period in question, has absolute value **≥ 2.0**. If nothing is unusual, the right answer is "within normal variation", not an invented cause. |
| **R11 Driver** | A segment is a driver only if **both** hold: its share of the total change is ≥ **15%** in absolute value, **and** its lift is at least **0.5 away from 1**. Lift = share of change / share of base. Lift near 1 means the segment just moved with everything else. **High lift with a small share is noise** (at brand level lifts of 5+ occur by chance every month). If the total change is under 2% of the base, do not look for drivers at all. The same rule applied to **net revenue** (R4) finds drivers that gross revenue cannot show, e.g. a returns problem; say which measure was used. |
| **R13 Customer type** | An order is **new** if it is the user's first order ever, by `orders.created_at`. Every later order is **returning**. |

## Traps in this dataset

| Rule | Definition |
|---|---|
| **R14 Two "traffic source" vocabularies** | `users.traffic_source` is the **acquisition** source: Search, Organic, Facebook, Display, Email. `sessions.traffic_source` is the **session** source: Email, Adwords, YouTube, Facebook, Organic. Revenue questions default to acquisition source; funnel questions default to session source. Always say which one was used. |
| **R15 Funnel** | The `sessions` table has one purchase session per order **item**, not per order. Non-purchase sessions are anonymous and flat at about 65,000 a year, so the purchase rate rises mechanically from 2% (2019) to 53% (2026). **Never compare conversion across time.** Compare it only across browser or session source within the same month. `saw_product` is 1 for every session and carries no information. |
| **R16 Fan-out** | Never join two one-to-many tables to `orders` inside one aggregate. It double counts silently: Aug 2026 revenue becomes 993,142 (1.91×) and nothing errors. |
| **R17 Missing dimension values** | 24 products have no brand (133 items, 4,666 of revenue all-time). A missing value is reported as the segment **`(unknown)`**, never dropped. Dropping it makes the brand segments stop adding up to total revenue, and pandas `groupby` does exactly that by default. |
| **R18 No discounts in the base data** | `sale_price` equals `products.retail_price` on 100% of items. Any "because of a promotion / discount" explanation on the untouched snapshot is invented. A planted scenario that lowers `sale_price` therefore leaves a real, findable trace: sale price below retail. |
| **R19 Email is not an identifier** | 15,167 duplicate emails in `users` (name-generator collisions). Identify a customer by `users.id`, never by email. Irrelevant to revenue questions; matters if a customer-facing flow is ever added. |
| **R21 Leakage** | Cancelled / returned share of a month is a share of **orders**. Broken down by a segment (e.g. distribution centre) it is the share of the segment's gross **revenue** that sits in cancelled / returned orders, because an order can span segments. Base data: about 15% cancelled and 10% returned everywhere, every month. |
| **R20 No calendar structure** | No seasonality (each calendar month's share of the year rises monotonically 6.6% → 10.2%, which is just growth), no weekday effect (6,434–6,610 orders per weekday over the last year), no hour-of-day effect. Delivery gaps are bounded uniforms (max 3 / 5 / 3 days). "Peak season", "weekend" and "delivery delays" are never valid explanations on the base data. |

## Borderline cases

R10 and R11 are thresholds, and thresholds have knife edges. Aug 2026 is one: asked
about on its own it is judged against the 24 months before August and scores
z = 1.99 (not unusual); asked about as part of the Jun–Aug window it is judged
against the 24 months before June and scores z = 2.06 (unusual). Both follow R10.
**Do not build an eval case whose truth depends on a value within 0.15 of a
threshold.** Planted scenarios should clear the threshold with a wide margin.

## Synonyms the agent should map

| User says | Means |
|---|---|
| revenue, sales, turnover, GMV, 收入, 销售额, 流水 | R3 revenue (gross) |
| net sales, after returns | R4 net revenue |
| basket size, ticket, 客单价 | R6 AOV |
| channel, source, 渠道 | ambiguous: see R14 |
| conversion, 转化率 | purchase rate per session, subject to R15 |
| new vs repeat, 新老客 | R13 |
