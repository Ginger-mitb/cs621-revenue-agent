-- Revenue by one product/user dimension for one month (R3, R2).
-- {dim_expr} is filled from a whitelist in test_kpi.py, never from user text:
--   category -> p.category      brand -> p.brand          department -> p.department
--   dist_center -> dc.name      acq_source -> u.traffic_source (R14)   country -> u.country
-- R17: a NULL dimension value is reported as '(unknown)', never dropped.
SELECT COALESCE({dim_expr}, '(unknown)') AS segment,
       SUM(oi.sale_price) AS revenue
FROM order_items oi
JOIN orders   o  USING (order_id)
JOIN products p  ON p.id  = oi.product_id
JOIN users    u  ON u.id  = o.user_id
JOIN distribution_centers dc ON dc.id = p.distribution_center_id
WHERE substr(o.created_at, 1, 7) = :month
GROUP BY 1
ORDER BY 2 DESC;
