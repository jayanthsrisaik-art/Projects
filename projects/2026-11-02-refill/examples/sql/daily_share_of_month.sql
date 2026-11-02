-- Each day's share of its month's net sales (reads a month-grain table).
SELECT d.region, d.net_cents, m.net_cents,
       CASE WHEN m.net_cents = 0 THEN NULL ELSE ROUND(1.0 * d.net_cents / m.net_cents, 6) END
FROM daily_region_sales d
JOIN monthly_region_sales m ON m.region = d.region AND m.part = :month
WHERE d.part = :p
