SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_on", YEAR) AS DATE) AS "ordered_on__year"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_on__year" DESC
