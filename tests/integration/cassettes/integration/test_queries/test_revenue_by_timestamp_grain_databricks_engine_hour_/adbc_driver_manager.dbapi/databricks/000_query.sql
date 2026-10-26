SELECT
  MEASURE("revenue") AS "revenue",
  (
    "ordered_at" - MAKE_DT_INTERVAL(0, 0, EXTRACT(MINUTE FROM "ordered_at"), EXTRACT(SECOND FROM "ordered_at"))
  ) AS "ordered_at__hour"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__hour" ASC
