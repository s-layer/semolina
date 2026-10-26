SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_on", WEEK) AS DATE) AS "ordered_on__week"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_on__week" DESC
