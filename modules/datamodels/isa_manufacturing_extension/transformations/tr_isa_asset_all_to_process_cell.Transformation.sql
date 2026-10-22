WITH site_space AS (
  SELECT site_name, site_space FROM `ice-cream-factory-db`.`config_site_space`
),
base AS (
  SELECT
    cast(a.external_id as string) AS external_id,
    cast(a.external_id as string) AS processCellId,
    cast(a.name as string) AS processCellName,
    from_json(a.metadata, 'map<string,string>') AS metadata_map,
    cast(a.parent_external_id as string) AS parentExternalId,
    cast(a.site as string) AS site_name
  FROM `ice-cream-factory-db`.`assets` a
  WHERE array_contains(a.labels, 'isa95:process_cell')
),
withSpace AS (
  SELECT b.*, ss.site_space
  FROM base b
  LEFT JOIN site_space ss ON b.site_name = ss.site_name
),
parsed AS (
  SELECT
    external_id, processCellId, processCellName, site_space,
    cast(metadata_map['type'] as string) AS process_cell_type,
    parentExternalId
  FROM withSpace
),
withRefs AS (
  SELECT
    external_id, processCellId, processCellName, process_cell_type, site_space,
    CASE
      WHEN parentExternalId IS NULL OR parentExternalId = '' THEN NULL
      ELSE node_reference(site_space, parentExternalId)
    END AS area
  FROM parsed
  WHERE site_space IS NOT NULL
)
SELECT
  external_id AS externalId,
  site_space  AS space,
  FIRST(processCellId, true) AS processCellId,
  FIRST(processCellName, true) AS processCellName,
  FIRST(process_cell_type, true) AS process_cell_type,
  FIRST(area, true) AS area
FROM withRefs
GROUP BY external_id, site_space