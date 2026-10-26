SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_at", DAY) AS DATE) AS "ordered_at__day"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__day" ASC
