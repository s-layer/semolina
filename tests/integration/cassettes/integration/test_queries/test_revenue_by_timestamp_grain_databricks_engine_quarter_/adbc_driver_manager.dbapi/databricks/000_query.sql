SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_at", QUARTER) AS DATE) AS "ordered_at__quarter"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__quarter" ASC
