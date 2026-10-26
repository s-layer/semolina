SELECT
  column_name,
  tag_name,
  tag_value
FROM "jaffle".information_schema.column_tags
WHERE
  schema_name = 'test_2c0e333b'
  AND table_name = 'annotated_view'
  AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_SPEC')
