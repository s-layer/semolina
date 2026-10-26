SELECT
  MEASURE("revenue") AS "revenue",
  (
    "ordered_at" - MAKE_DT_INTERVAL(0, 0, 0, EXTRACT(SECOND FROM "ordered_at"))
  ) AS "ordered_at__minute"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__minute" ASC
