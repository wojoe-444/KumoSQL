INSERT INTO `shop-legacy.sandbox.customer_totals` (customer_id, lifetime_amount)
SELECT customer_id, SUM(amount)
FROM `shop-legacy.archive.orders_2019`
GROUP BY customer_id;
