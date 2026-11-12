WITH site_space AS (
  SELECT site_name, site_space FROM `ice-cream-factory-db`.`config_site_space`
),
base AS (
  SELECT
    cast(a.external_id as string) AS external_id,
    cast(a.external_id as string) AS productionLineId,
    cast(a.name as string) AS productionLineName,
    from_json(a.metadata, 'map<string,string>') AS metadata_map,
    cast(a.parent_external_id as string) AS parentExternalId,
    cast(a.site as string) AS site_name
  FROM `ice-cream-factory-db`.`assets` a
  WHERE array_contains(a.labels, 'isa95:production_line')
),
withSpace AS (
  SELECT b.*, ss.site_space
  FROM base b
  LEFT JOIN site_space ss ON b.site_name = ss.site_name
),
parsed AS (
  SELECT
    external_id, productionLineId, productionLineName, site_space, site_name,
    cast(metadata_map['type'] as string) AS production_line_type,
    cast(metadata_map['src'] as string) AS src,
    to_json(map_filter(metadata_map, (k, v) -> k NOT IN ('type', 'site', 'src'))) AS metadata,
    parentExternalId
  FROM withSpace
),
withRefs AS (
  SELECT
    external_id, productionLineId, productionLineName, production_line_type,
    site_name, src, metadata, site_space,
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
  FIRST(productionLineId, true) AS productionLineId,
  FIRST(productionLineName, true) AS productionLineName,
  FIRST(production_line_type, true) AS production_line_type,
  FIRST(site_name, true) AS site,        -- <- this was missing: the actual `site` property output
  FIRST(src, true) AS src,
  FIRST(metadata, true) AS metadata,
  FIRST(area, true) AS area
FROM withRefs
GROUP BY external_id, site_space