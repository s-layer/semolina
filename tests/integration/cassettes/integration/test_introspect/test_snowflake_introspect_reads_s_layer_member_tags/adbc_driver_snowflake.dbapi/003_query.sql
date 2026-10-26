SELECT
  member,
  tag_name,
  tag_value
FROM (
  SELECT
    'MARGIN' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_23FC50FD"."TAGGED_VIEW"!"MARGIN"', 'SEMANTIC METRIC')
  )
  UNION ALL
  SELECT
    'COST' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_23FC50FD"."TAGGED_VIEW"!"SALES_DATA"."COST"', 'SEMANTIC METRIC')
  )
  UNION ALL
  SELECT
    'COUNTRY' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES(
      '"DEMO"."TEST_23FC50FD"."TAGGED_VIEW"!"SALES_DATA"."COUNTRY"',
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
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_23FC50FD"."TAGGED_VIEW"!"SALES_DATA"."REGION"', 'SEMANTIC DIMENSION')
  )
  UNION ALL
  SELECT
    'REVENUE' AS member,
    tag_name,
    tag_value,
    level,
    domain
  FROM TABLE(
    "DEMO".information_schema.TAG_REFERENCES('"DEMO"."TEST_23FC50FD"."TAGGED_VIEW"!"SALES_DATA"."REVENUE"', 'SEMANTIC METRIC')
  )
)
WHERE
  level = domain AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_LABEL', 'S_LAYER_SPEC')
