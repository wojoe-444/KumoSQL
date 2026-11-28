-- Daily orders: two steps that write tables, through a temporary table.
DECLARE run_date DATE DEFAULT CURRENT_DATE();

CREATE TEMP TABLE paid AS
SELECT order_id, customer_id, amount, DATE(created_at) AS order_day
FROM raw.orders
WHERE status = 'paid';

CREATE OR REPLACE TABLE orders_clean AS
SELECT order_id, customer_id, amount, order_day
FROM paid
WHERE order_day <= run_date;

CREATE OR REPLACE TABLE sandbox.orders_daily AS
SELECT order_day, SUM(amount) AS revenue
FROM orders_clean
GROUP BY order_day;
