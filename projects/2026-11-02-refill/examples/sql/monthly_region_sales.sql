-- Month rollup; :p_start/:p_end are clipped to the pipeline range.
SELECT region, SUM(orders), SUM(gross_cents), SUM(refund_cents), SUM(net_cents)
FROM daily_region_sales
WHERE part BETWEEN :p_start AND :p_end
GROUP BY region
