SELECT
  column_name,
  tag_name,
  tag_value
FROM "jaffle".information_schema.column_tags
WHERE
  schema_name = 'test_e55d05ec'
  AND table_name = 'typed_view'
  AND tag_name IN ('S_LAYER_KIND', 'S_LAYER_SPEC')
