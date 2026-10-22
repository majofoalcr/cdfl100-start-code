WITH base AS (
  SELECT
    cast(external_id as string) AS external_id,
    cast(external_id as string) AS enterpriseId,
    cast(name as string) AS enterpriseName
  FROM `{{ rawDatabase }}`.`assets`
  WHERE array_contains(labels, 'isa95:enterprise')
)
SELECT
  external_id AS externalId,
  FIRST(enterpriseId, true) AS enterpriseId,
  FIRST(enterpriseName, true) AS enterpriseName
FROM base
GROUP BY external_id
