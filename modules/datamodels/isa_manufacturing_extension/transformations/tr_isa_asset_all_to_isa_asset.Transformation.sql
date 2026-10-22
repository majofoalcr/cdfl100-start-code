WITH site_space AS (
  SELECT
    cast(site_name as string)  AS site_name,
    cast(site_space as string) AS site_space
  FROM `ice-cream-factory-db`.`config_site_space`
),
base AS (
  SELECT
    cast(a.external_id as string)         AS external_id,
    cast(a.name as string)                AS name,
    cast(a.parent_external_id as string)  AS parentExternalId,
    cast(a.description as string)         AS description,
    cast(a.site as string)                AS site_name
  FROM `ice-cream-factory-db`.`assets` a
),
withSpace AS (
  SELECT b.*, ss.site_space
  FROM base b
  LEFT JOIN site_space ss ON b.site_name = ss.site_name
),
withRefs AS (
  SELECT
    external_id, name, description, site_space,
    CASE
      WHEN parentExternalId IS NULL OR parentExternalId = '' THEN NULL
      ELSE node_reference(site_space, parentExternalId)
    END AS parent
  FROM withSpace
  WHERE site_space IS NOT NULL
)
SELECT
  external_id AS externalId,
  site_space  AS space,
  FIRST(name, true) AS name,
  FIRST(description, true) AS description,
  FIRST(parent, true) AS parent
FROM withRefs
GROUP BY external_id, site_space