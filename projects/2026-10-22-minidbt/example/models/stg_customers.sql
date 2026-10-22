SELECT customer_id, name, country FROM {{ ref('raw_customers') }}
