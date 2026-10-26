SELECT
  column_name,
  tag_name,
  tag_value
FROM "jaffle".information_schema.column_tags
WHERE
  schema_name = 'test_7c6e4d4b'
  AND table_name = 'sales_view'
  AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_SPEC')
