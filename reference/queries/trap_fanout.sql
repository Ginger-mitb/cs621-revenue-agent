-- WRONG ON PURPOSE. Violates R16: a second one-to-many join to the same order
-- multiplies every row. Runs without error and returns a plausible number
-- (993,142 for 2026-08, 1.91x the truth). This is "plausible garbage" in SQL form.
SELECT SUM(oi.sale_price) AS revenue
FROM order_items oi
JOIN orders      o   USING (order_id)
JOIN order_items oi2 USING (order_id)
WHERE substr(o.created_at, 1, 7) = :month;
