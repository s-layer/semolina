SELECT
  member,
  tag_name,
  tag_value
FROM (
  SELECT
    'COST' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_C7B41CE8"."SALES_VIEW"!"SALES_DATA"."COST"', 'SEMANTIC METRIC')
  )
  UNION ALL
  SELECT
    'COUNTRY' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_C7B41CE8"."SALES_VIEW"!"SALES_DATA"."COUNTRY"', 'SEMANTIC DIMENSION')
  )
  UNION ALL
  SELECT
    'IS_ONLINE' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES(
      '"DEMO"."TEST_C7B41CE8"."SALES_VIEW"!"SALES_DATA"."IS_ONLINE"',
      'SEMANTIC DIMENSION'
    )
  )
  UNION ALL
  SELECT
    'REGION' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_C7B41CE8"."SALES_VIEW"!"SALES_DATA"."REGION"', 'SEMANTIC DIMENSION')
  )
  UNION ALL
  SELECT
    'REVENUE' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_C7B41CE8"."SALES_VIEW"!"SALES_DATA"."REVENUE"', 'SEMANTIC METRIC')
  )
)
WHERE
  level = domain AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_LABEL', 'S_LAYER_SPEC')
