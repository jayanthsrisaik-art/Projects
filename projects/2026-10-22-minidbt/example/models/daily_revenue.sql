{{ config(materialized='incremental', unique_key='order_date') }}
SELECT order_date, SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) AS revenue, COUNT(*) AS orders
FROM {{ ref('stg_orders') }}
{% if is_incremental %}
WHERE order_date >= (SELECT MAX(order_date) FROM {{ this }})
{% endif %}
GROUP BY order_date
