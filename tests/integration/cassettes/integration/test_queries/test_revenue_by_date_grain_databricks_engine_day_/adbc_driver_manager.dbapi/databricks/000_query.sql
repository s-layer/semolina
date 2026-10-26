SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_on", DAY) AS DATE) AS "ordered_on__day"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_on__day" DESC
