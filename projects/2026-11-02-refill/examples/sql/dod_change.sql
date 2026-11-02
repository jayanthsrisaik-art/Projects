-- Day-over-day change in net sales per region.
SELECT t.region, t.net_cents, COALESCE(y.net_cents, 0), t.net_cents - COALESCE(y.net_cents, 0)
FROM daily_region_sales t
LEFT JOIN daily_region_sales y ON y.region = t.region AND y.part = :prev
WHERE t.part = :p
