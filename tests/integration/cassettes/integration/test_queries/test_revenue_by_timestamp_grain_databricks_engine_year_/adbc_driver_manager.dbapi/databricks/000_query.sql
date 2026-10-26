SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_at", YEAR) AS DATE) AS "ordered_at__year"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__year" ASC
