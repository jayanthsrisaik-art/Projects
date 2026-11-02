-- Running total: yesterday's cumulative value (self-dependency) + today's net.
SELECT region, SUM(v)
FROM (
  SELECT region, cum_net_cents AS v FROM cumulative_net WHERE part = :prev
  UNION ALL
  SELECT region, net_cents AS v FROM daily_region_sales WHERE part = :p
)
GROUP BY region
