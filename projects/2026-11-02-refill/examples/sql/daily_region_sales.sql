-- Net sales per region for one day. Refunds are booked on the refund date.
WITH o AS (
  SELECT c.region, COUNT(*) AS orders, SUM(x.amount_cents) AS gross_cents
  FROM raw_orders x JOIN dim_customer c ON c.customer_id = x.customer_id
  WHERE x.part = :p
  GROUP BY c.region
), r AS (
  SELECT c.region, SUM(x.amount_cents) AS refund_cents
  FROM raw_refunds x JOIN dim_customer c ON c.customer_id = x.customer_id
  WHERE x.part = :p
  GROUP BY c.region
), regions AS (SELECT region FROM o UNION SELECT region FROM r)
SELECT g.region,
       COALESCE(o.orders, 0),
       COALESCE(o.gross_cents, 0),
       COALESCE(r.refund_cents, 0),
       COALESCE(o.gross_cents, 0) - COALESCE(r.refund_cents, 0)
FROM regions g
LEFT JOIN o ON o.region = g.region
LEFT JOIN r ON r.region = g.region
