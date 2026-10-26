SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_on", QUARTER) AS DATE) AS "ordered_on__quarter"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_on__quarter" DESC
