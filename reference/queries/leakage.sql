-- Share of ORDERS cancelled and returned in one month. Counted on orders, not items.
SELECT AVG(status = 'Cancelled') AS cancelled,
       AVG(status = 'Returned')  AS returned
FROM orders
WHERE substr(created_at, 1, 7) = :month;
