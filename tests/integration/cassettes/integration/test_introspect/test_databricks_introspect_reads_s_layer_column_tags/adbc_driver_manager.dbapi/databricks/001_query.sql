SELECT
  column_name,
  tag_name,
  tag_value
FROM "jaffle".information_schema.column_tags
WHERE
  schema_name = 'test_6a4455f6'
  AND table_name = 'tagged_view'
  AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_SPEC')
