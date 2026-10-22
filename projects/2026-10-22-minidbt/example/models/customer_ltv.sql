{{ config(materialized='table') }}
SELECT c.customer_id, c.name, c.country, COALESCE(SUM(o.amount), 0) AS lifetime_paid
FROM {{ ref('stg_customers') }} c
LEFT JOIN {{ ref('stg_orders') }} o ON o.customer_id = c.customer_id AND o.status = 'paid'
GROUP BY c.customer_id, c.name, c.country
