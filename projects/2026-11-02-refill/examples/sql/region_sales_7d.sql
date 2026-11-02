-- Trailing 7-day window ending on :p (inclusive).
SELECT region, SUM(orders), SUM(net_cents), COUNT(DISTINCT part)
FROM daily_region_sales
WHERE part BETWEEN date(:p, '-6 days') AND :p
GROUP BY region
