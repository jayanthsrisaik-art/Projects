{{ config(materialized='view') }}
SELECT order_id, customer_id, status, CAST(amount AS REAL) AS amount, order_date
FROM {{ ref('raw_orders') }}
WHERE status != 'pending'
