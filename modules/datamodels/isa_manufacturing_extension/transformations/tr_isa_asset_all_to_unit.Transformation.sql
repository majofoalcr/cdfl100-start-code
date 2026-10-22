WITH site_space AS (
  SELECT site_name, site_space FROM `ice-cream-factory-db`.`config_site_space`
),
base AS (
  SELECT
    cast(a.external_id as string) AS external_id,
    cast(a.external_id as string) AS unitId,
    cast(a.name as string) AS unitName,
    from_json(a.metadata, 'map<string,string>') AS metadata_map,
    cast(a.parent_external_id as string) AS parentExternalId,
    cast(a.site as string) AS site_name
  FROM `ice-cream-factory-db`.`assets` a
  WHERE array_contains(a.labels, 'isa95:unit')
),
withSpace AS (
  SELECT b.*, ss.site_space
  FROM base b
  LEFT JOIN site_space ss ON b.site_name = ss.site_name
),
parsed AS (
  SELECT
    external_id, unitId, unitName, site_space, site_name AS site,
    cast(metadata_map['type'] as string) AS unit_class,
    cast(metadata_map['src'] as string) AS src,
    cast(metadata_map['Machine type'] as string) AS machine_type,
    cast(metadata_map['Function'] as string) AS function,
    cast(coalesce(metadata_map['Machine dimensions'], metadata_map['Dimensions (L x W x H), mm']) as string) AS dimensions,
    cast(coalesce(metadata_map['Machine weight'], metadata_map['Weight, kg'], metadata_map['Weight']) as string) AS weight,
    cast(metadata_map['Output, bricks/min'] as string) AS output,
    cast(coalesce(metadata_map['Power'], metadata_map['Rated power input, kVA'], metadata_map['Total installed power (incl. fans) (kW (HP))']) as string) AS power,
    cast(metadata_map['Model'] as string) AS model,
    cast(
      replace(regexp_extract(trim(metadata_map['Capacity']), '^([0-9.,]+)', 1), ',', '')
      as double
    ) AS capacity,
    trim(regexp_extract(trim(metadata_map['Capacity']), '^[0-9.,]+\\s*(.*)$', 1)) AS capacity_unit,
    to_json(map_filter(metadata_map, (k, v) -> k NOT IN (
      'type', 'site', 'src', 'Machine type', 'Function', 'Machine dimensions',
      'Dimensions (L x W x H), mm', 'Machine weight', 'Weight, kg', 'Weight',
      'Output, bricks/min', 'Power', 'Rated power input, kVA',
      'Total installed power (incl. fans) (kW (HP))', 'Model', 'Capacity'
    ))) AS metadata,
    parentExternalId
  FROM withSpace
),
parents AS (
  SELECT
    cast(external_id as string) AS external_id,
    array_contains(labels, 'isa95:process_cell') AS is_process_cell,
    array_contains(labels, 'isa95:production_line') AS is_production_line
  FROM `ice-cream-factory-db`.`assets`
),
withRefs AS (
  SELECT
    parsed.external_id, parsed.unitId, parsed.unitName, parsed.unit_class, parsed.site,
    parsed.src, parsed.machine_type, parsed.function, parsed.dimensions, parsed.weight,
    parsed.output, parsed.power, parsed.model, parsed.capacity, parsed.capacity_unit,
    parsed.metadata, parsed.site_space,
    CASE
      WHEN parsed.parentExternalId IS NULL OR parsed.parentExternalId = '' THEN NULL
      WHEN parents.is_process_cell THEN node_reference(parsed.site_space, parsed.parentExternalId)
      ELSE NULL
    END AS processCell,
    CASE
      WHEN parsed.parentExternalId IS NULL OR parsed.parentExternalId = '' THEN NULL
      WHEN parents.is_production_line THEN node_reference(parsed.site_space, parsed.parentExternalId)
      ELSE NULL
    END AS productionLine
  FROM parsed
  LEFT JOIN parents ON parsed.parentExternalId = parents.external_id
  WHERE parsed.site_space IS NOT NULL
)
SELECT
  external_id AS externalId,
  site_space  AS space,
  FIRST(unitId, true) AS unitId,
  FIRST(unitName, true) AS unitName,
  FIRST(unit_class, true) AS unit_class,
  FIRST(site, true) AS site,
  FIRST(src, true) AS src,
  FIRST(machine_type, true) AS machine_type,
  FIRST(function, true) AS function,
  FIRST(dimensions, true) AS dimensions,
  FIRST(weight, true) AS weight,
  FIRST(output, true) AS output,
  FIRST(power, true) AS power,
  FIRST(model, true) AS model,
  FIRST(capacity, true) AS capacity,
  FIRST(capacity_unit, true) AS capacity_unit,
  FIRST(metadata, true) AS metadata,
  FIRST(processCell, true) AS processCell,
  FIRST(productionLine, true) AS productionLine
FROM withRefs
GROUP BY external_id, site_space