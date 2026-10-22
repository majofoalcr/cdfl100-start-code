SELECT
  timeseries.externalId,
  timeseries.isStep,
  timeseries.type,
  timeseries.site,
  timeseries.space,
  CASE
    WHEN assets.externalId IS NOT NULL
    THEN array(node_reference(assets.space, assets.externalId))
    ELSE NULL
  END AS assets
FROM cdf_data_models("dm_dom_isa_manufacturing", "ISA_Manufacturing_DOM", "{{ enterprise_dm_version }}", "ISATimeSeries") AS timeseries
LEFT JOIN
  cdf_data_models("dm_dom_isa_manufacturing", "ISA_Manufacturing_DOM", "{{ enterprise_dm_version }}", "ISAAsset") AS assets
ON
  split_part(timeseries.externalId, ":", 1) = assets.externalId