SELECT
  MEASURE("revenue") AS "revenue",
  (
    "ordered_at" - MAKE_DT_INTERVAL(0, 0, 0, EXTRACT(SECOND FROM "ordered_at") % 1)
  ) AS "ordered_at__second"
FROM "orders_view"
GROUP BY ALL
ORDER BY
  "ordered_at__second" ASC
