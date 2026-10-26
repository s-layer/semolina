SELECT
  column_name,
  tag_name,
  tag_value
FROM "jaffle".information_schema.column_tags
WHERE
  schema_name = 'test_bb086d84'
  AND table_name = 'formatted_view'
  AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_SPEC')
