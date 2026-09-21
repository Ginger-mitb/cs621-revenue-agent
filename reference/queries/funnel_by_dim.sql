-- Session funnel for ONE month, split by a session dimension (R15).
-- {dim_expr} is whitelisted: traffic_source (the SESSION source, R14) or browser.
-- Valid use: compare segments within this month. Invalid use: compare these
-- rates with another month's -- the purchase rate rises mechanically over time.
SELECT {dim_expr}                     AS segment,
       COUNT(*)                       AS sessions,
       AVG(added_to_cart)             AS cart_rate,
       AVG(purchased)                 AS purchase_rate
FROM sessions
WHERE substr(started_at, 1, 7) = :month
GROUP BY 1
ORDER BY 2 DESC;
