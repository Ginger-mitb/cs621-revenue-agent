-- Core monthly KPIs: revenue (R3), net revenue (R4), orders and items (R5), profit (R7).
-- Month comes from ORDERS.created_at (R2).
-- Only ONE one-to-many table (order_items) is joined to orders, so nothing fans out (R16).
WITH item AS (
    SELECT substr(o.created_at, 1, 7) AS month,
           o.order_id, o.status, oi.sale_price, p.cost
    FROM order_items oi
    JOIN orders   o USING (order_id)
    JOIN products p ON p.id = oi.product_id
)
SELECT month,
       SUM(sale_price)                                                                  AS revenue,
       SUM(CASE WHEN status NOT IN ('Cancelled', 'Returned') THEN sale_price ELSE 0 END) AS net_revenue,
       COUNT(DISTINCT order_id)                                                         AS orders,
       COUNT(*)                                                                         AS items,
       SUM(sale_price - cost)                                                           AS profit
FROM item
GROUP BY month
ORDER BY month;
