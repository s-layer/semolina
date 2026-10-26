SELECT
  MEASURE("revenue") AS "revenue",
  CAST(TIMESTAMP_TRUNC("ordered_at", WEEK) AS DATE) AS "ordered_at__week"
FROM "orders_view"
WHERE
  "ordered_at" >= ?
GROUP BY ALL
ORDER BY
  "ordered_at__week" ASC
