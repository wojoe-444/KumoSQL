CREATE OR REPLACE TABLE customer_totals AS
SELECT customer_id, SUM(amount) AS lifetime_amount
FROM orders_clean
GROUP BY customer_id;
