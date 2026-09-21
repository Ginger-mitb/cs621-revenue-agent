-- Revenue from new vs returning customers for one month (R13).
-- An order is "new" when it is the user's first order ever, judged on the WHOLE
-- history, not just the month being asked about.
WITH first_order AS (
    SELECT user_id, MIN(created_at) AS first_at
    FROM orders
    GROUP BY user_id
)
SELECT CASE WHEN o.created_at = f.first_at THEN 'new' ELSE 'returning' END AS segment,
       SUM(oi.sale_price) AS revenue
FROM order_items oi
JOIN orders      o USING (order_id)
JOIN first_order f ON f.user_id = o.user_id
WHERE substr(o.created_at, 1, 7) = :month
GROUP BY 1
ORDER BY 1;
