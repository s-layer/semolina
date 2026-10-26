SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_at", MONTH) AS DATE) AS "ordered_at__month"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__month" ASC
