-- WRONG ON PURPOSE. Violates R2: attributes revenue to the month of
-- order_items.created_at instead of orders.created_at.
-- Kept so the test suite pins the wrong number (516,172 for 2026-08) next to the
-- right one (519,370), and so an eval case can recognise an agent that made
-- this exact mistake.
SELECT SUM(sale_price) AS revenue
FROM order_items
WHERE substr(created_at, 1, 7) = :month;
