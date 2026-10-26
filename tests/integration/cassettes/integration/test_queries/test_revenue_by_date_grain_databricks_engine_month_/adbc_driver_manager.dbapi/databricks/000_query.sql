SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_on", MONTH) AS DATE) AS "ordered_on__month"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_on__month" DESC
