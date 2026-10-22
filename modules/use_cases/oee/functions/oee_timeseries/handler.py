from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict

from cognite.client import CogniteClient
from cognite.client.data_classes.data_modeling import (
    ContainerId,
    NodeApply,
    NodeId,
    NodeOrEdgeData,
    ViewId,
)
from cognite.client.data_classes.data_modeling.cdm.v1 import CogniteTimeSeries, CogniteTimeSeriesApply
from cognite.client.data_classes.filters import Equals
from cognite.client.exceptions import CogniteNotFoundError

import numpy as np

from cognite.client.config import global_config
global_config.disable_pypi_version_check = True


# ---------------------------------------------------------------------------
# Data model / space configuration
#
# The Ice Cream Factory solution is modeled on the ISA-95/ISA-88 Core Data
# Model (CDM) extension ("isa_manufacturing_extension"), NOT the legacy
# asset-centric model. There is no CogniteAsset traversal anymore - the
# Site -> Area -> ProcessCell -> Unit hierarchy is expressed through CDM
# views (Site, Unit, ISAAsset, ...) in the schema space below.
#
# The OEE source time series (count, good, status, planned_status) are
# contextualized to Units only (via the "tr_timeseries_all_to_isa_assets"
# transformation, which links CogniteTimeSeries.assets -> the ISAAsset
# representing each Unit) - not to Equipment. Every ISATimeSeries instance
# also carries a denormalized "site" property (sourced from that same
# Unit) specifically so consumers like this function don't have to walk
# the asset hierarchy to find the time series that belong to a site.
#
# The calculated Availability/Performance/Quality/OEE time series are
# written back into that same per-site instance space - there is no
# separate shared OEE space.
# ---------------------------------------------------------------------------

# Schema space: owns the containers/views for the ISA-95/ISA-88 extension.
ENTERPRISE_SPACE = "dm_dom_isa_manufacturing"

# The ISATimeSeries *container* (which holds the "site" property) has a
# different externalId than the ISATimeSeries *view* - see
# dm_dom_isa_manufacturing_ISATimeSeries.Container.yaml.
ISA_TIME_SERIES_CONTAINER_ID = ContainerId(ENTERPRISE_SPACE, "dm_dom_isa_manufacturing_ISATimeSeries")

# Fallback view version, only used if the function is invoked without a
# "viewVersion" key in its data payload. In deployment, the Toolkit
# module variable `viewVersion` (see config.dev.yaml /
# isa_manufacturing_extension's `enterprise_dm_version`) should be
# templated into this function's schedule/call data, e.g.:
#
#   data:
#     viewVersion: {{ viewVersion }}
#     lookback_minutes: 60
#
# so the deployed view version always matches whatever is pinned for the
# environment rather than being hardcoded here.
DEFAULT_VIEW_VERSION = "v1"

# Note: there is no separate OEE instance space. The calculated
# Availability/Performance/Quality/OEE time series are written back into
# the same per-site instance space as their source count/good/status/
# planned_status time series (see `site_space`), not into a shared space.

# Raw ICAPI time series (count, good, status, planned_status) are no longer
# all written into a single space. Each site owns its own instance space, so
# a query can be scoped to (and access-controlled at) the site level.
SITE_DISPLAY_NAMES: Dict[str, str] = {
    "Houston": "Houston",
    "Oslo": "Oslo",
    "Kuala_Lumpur": "Kuala Lumpur",
    "Hannover": "Hannover",
    "Nuremberg": "Nuremberg",
    "Marseille": "Marseille",
    "Sao_Paulo": "Sao Paulo",
    "Chicago": "Chicago",
    "Rotterdam": "Rotterdam",
    "London": "London",
}

SOURCE_TIME_SERIES_SUFFIXES = ("count", "good", "status", "planned_status")


def site_space(site_key: str) -> str:
    """Per-site instance space, e.g. 'Kuala_Lumpur' -> 'kuala_lumpur_sp'."""
    return f"{site_key.lower()}_sp"


def _is_source_time_series(external_id: str) -> bool:
    return external_id.rsplit(":", 1)[-1] in SOURCE_TIME_SERIES_SUFFIXES


def _first_or_self(value):
    """Handle scalars and nested one-item arrays from different SDK versions."""
    while isinstance(value, (list, tuple, np.ndarray)):
        if len(value) == 0:
            return None
        value = value[0]
    return value


def _timestamp_ms(value) -> int | None:
    value = _first_or_self(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    return int(value)


def _fill_binary_step_series(
    dataframe,
    column: str,
    latest_datapoint,
    window_start: int,
) -> bool:
    """Fill a binary step series, carrying in its latest value before the window."""
    if column not in dataframe.columns:
        return False

    series = dataframe[column].ffill()
    latest_timestamp = _timestamp_ms(getattr(latest_datapoint, "timestamp", None))
    latest_value = _first_or_self(getattr(latest_datapoint, "value", None))

    if (
        latest_timestamp is not None
        and latest_timestamp < window_start
        and latest_value is not None
    ):
        leading_value = latest_value
    else:
        first_valid_index = series.first_valid_index()
        if first_valid_index is None:
            return False
        first_valid_value = series.loc[first_valid_index]
        leading_value = 1.0 if first_valid_value == 0.0 else 0.0

    dataframe[column] = series.fillna(leading_value)
    return True


def _build_calculated_time_series(
    source_space: str,
    external_id: str,
    name: str,
    site_key: str,
) -> NodeApply:
    typed_apply = CogniteTimeSeriesApply(
        space=source_space,
        external_id=external_id,
        name=name,
        is_step=False,
        time_series_type="numeric",
    )
    return NodeApply(
        space=typed_apply.space,
        external_id=typed_apply.external_id,
        sources=[
            *typed_apply.sources,
            NodeOrEdgeData(
                source=ISA_TIME_SERIES_CONTAINER_ID,
                properties={"site": SITE_DISPLAY_NAMES[site_key]},
            ),
        ],
    )


def get_time_series_for_site(client: CogniteClient, site_key: str, view_version: str):
    """
    Return every CogniteTimeSeries instance that belongs to a given site.

    Under the ISA-95 Core Data Model extension we don't need to retrieve a
    CogniteAsset sub-tree and then filter time series by a "path" prefix or
    an "assets" containment filter. Instead, ISATimeSeries carries a
    denormalized "site" text property (see
    isa_manufacturing_extension/data_modeling/containers/
    dm_dom_isa_manufacturing_ISATimeSeries.Container.yaml) precisely so this
    kind of per-site lookup doesn't require a hierarchy walk.

    Note this also means the query is no longer scoped to a single space:
    each site's time series live in their own instance space (see
    `site_space`), so we filter on the "site" property across spaces rather
    than passing a single `space=` argument.

    `view_version` is the parameterized ISATimeSeries view version
    (`viewVersion` in the Toolkit config) - see DEFAULT_VIEW_VERSION.
    """
    site_display_name = SITE_DISPLAY_NAMES[site_key]
    isa_time_series_view = ViewId(ENTERPRISE_SPACE, "ISATimeSeries", view_version)

    site_filter = Equals(
        property=[
            isa_time_series_view.space,
            f"{isa_time_series_view.external_id}/{isa_time_series_view.version}",
            "site",
        ],
        value=site_display_name,
    )

    time_series = client.data_modeling.instances.list(
        instance_type=CogniteTimeSeries,
        filter=site_filter,
        limit=None,
    )
    time_series = [
        time_series_item
        for time_series_item in time_series
        if _is_source_time_series(time_series_item.external_id)
    ]

    if not time_series:
        print(
            f"----No CogniteTimeSeries found for site {site_display_name}!----\n"
            f"    Confirm the ICAPI hosted extractor has ingested data into "
            f"'{site_space(site_key)}' and that the 'site' property is populated "
            f"on the ISATimeSeries/{view_version} view."
        )

    return time_series


def handle(client: CogniteClient, data: Dict[str, Any] = {}) -> None:
    lookback_minutes = None
    sites = None
    view_version = DEFAULT_VIEW_VERSION

    if data:
        lookback_minutes = timedelta(minutes=data.get("lookback_minutes", 60)).total_seconds() * 1000
        sites = data.get("sites")
        # Parameterized ISATimeSeries view version. This should be wired up
        # from the Toolkit's `viewVersion` module variable at deploy time
        # (templated into this function's schedule/call data), rather than
        # hardcoded, so the function always resolves the view version that
        # matches the environment it's running in.
        view_version = data.get("viewVersion", DEFAULT_VIEW_VERSION)

    all_sites = list(SITE_DISPLAY_NAMES.keys())

    lookback_minutes = lookback_minutes or timedelta(minutes=60).total_seconds() * 1000
    sites = sites or all_sites

    print(f"Processing datapoints for these sites: {sites} (ISATimeSeries view version: {view_version})")
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(process_site, client, lookback_minutes, site, view_version) for site in sites
        ]
        for f in futures:
            f.result()


def process_site(client, lookback_minutes, site_key, view_version: str = DEFAULT_VIEW_VERSION):
    timeseries = get_time_series_for_site(client, site_key, view_version)
    if not timeseries:
        return

    # All time series for a site live in that site's own instance space
    # (see SITE_DISPLAY_NAMES / site_space). We read it back off the
    # returned instances rather than hardcoding it, so this keeps working
    # even if that convention changes.
    source_space = timeseries[0].space

    asset_eids = list({item.external_id.rsplit(":", 1)[0] for item in timeseries})
    instance_ids = [NodeId(space=ts.space, external_id=ts.external_id) for ts in timeseries]
    all_latest_dps = client.time_series.data.retrieve_latest(instance_id=instance_ids)

    # Organize latest datapoints by equipment for alignment
    assets_dps = {
        external_id: [
            latest_dp
            for latest_dp in all_latest_dps
            if latest_dp.instance_id.external_id.rsplit(":", 1)[0] == external_id
        ]
        for external_id in asset_eids
    }

    asset_inputs = {}
    assets_by_window_start = {}
    for asset, latest_dps in assets_dps.items():
        latest_by_suffix = {
            latest_dp.instance_id.external_id.rsplit(":", 1)[-1]: latest_dp
            for latest_dp in latest_dps
        }
        count_and_good_timestamps = [
            _timestamp_ms(getattr(latest_by_suffix.get(suffix), "timestamp", None))
            for suffix in ("count", "good")
        ]
        if any(timestamp is None for timestamp in count_and_good_timestamps):
            print(f"Failed to find count/good datapoints for {asset}")
            continue

        end = min(count_and_good_timestamps)
        window_start = end - lookback_minutes
        asset_inputs[asset] = (end, window_start)
        assets_by_window_start.setdefault(window_start, []).append(asset)

    carry_ins_by_window_start = {}
    for window_start, assets in assets_by_window_start.items():
        state_instance_ids = [
            NodeId(space=source_space, external_id=f"{asset}:{suffix}")
            for asset in assets
            for suffix in ("status", "planned_status")
        ]
        state_datapoints = client.time_series.data.retrieve_latest(
            instance_id=state_instance_ids,
            before=window_start,
            ignore_unknown_ids=True,
        )
        carry_ins_by_window_start[window_start] = {
            datapoint.instance_id.external_id: datapoint
            for datapoint in state_datapoints
        }

    for asset, (end, window_start) in asset_inputs.items():
        print(f"Calculating OEE for {asset}")
        count_node = f"NodeId({source_space}, {asset}:count)"
        good_node = f"NodeId({source_space}, {asset}:good)"
        status_node = f"NodeId({source_space}, {asset}:status)"
        planned_status_node = f"NodeId({source_space}, {asset}:planned_status)"
        carry_ins = carry_ins_by_window_start[window_start]

        if end:
            source_instance_ids = [
                NodeId(space=source_space, external_id=f"{asset}:{suffix}")
                for suffix in SOURCE_TIME_SERIES_SUFFIXES
            ]
            dps_df = client.time_series.data.retrieve_dataframe(
                instance_id=source_instance_ids,
                start=window_start,
                end=end,
                aggregates=["sum"],
                granularity="1m",
                include_aggregate_name=False,
                limit=None
            )

            # Frontfill because "planned_status" and "status" only have datapoints when the value changes
            dps_df = dps_df.ffill()

            if not _fill_binary_step_series(
                dps_df,
                planned_status_node,
                carry_ins.get(f"{asset}:planned_status"),
                window_start,
            ):
                print(f"Failed to find datapoints for {planned_status_node}")
                continue

            if not _fill_binary_step_series(
                dps_df,
                status_node,
                carry_ins.get(f"{asset}:status"),
                window_start,
            ):
                print(f"Failed to find datapoints for {status_node}")
                continue

            count_dps = dps_df[count_node]
            good_dps = dps_df[good_node]
            status_dps = dps_df[status_node]
            planned_status_dps = dps_df[planned_status_node]

            total_items = len(count_dps)

            if (
                total_items != len(good_dps)
                or total_items != len(status_dps)
                or total_items != len(planned_status_dps)
            ):
                # We expect ALL dependent timeseries to have the exact same number of datapoints
                # for the specified time range for the calculation to execute.
                print(
                    f"""{asset}: Unable to retrieve datapoints for all required OEE timeseries (count, good, status, planned_status)
                    between {end - lookback_minutes} and {end}. Ensure that data is available for the time range specified."""
                )

            # Calculate the components of OEE
            off_spec_node = f"{asset}:off_spec"
            quality_node = f"{asset}:quality"
            performance_node = f"{asset}:performance"
            availability_node = f"{asset}:availability"
            oee_node = f"{asset}:oee"

            dps_df[off_spec_node] = count_dps - good_dps
            dps_df[quality_node] = good_dps / count_dps
            dps_df[performance_node] = (count_dps / status_dps) / (60.0 / 3.0)
            dps_df[availability_node] = status_dps / planned_status_dps

            dps_df[oee_node] = dps_df[quality_node] * dps_df[performance_node] * dps_df[availability_node]

            # Fill in the divide by zeros
            dps_df = dps_df.fillna(value=0.0)
            dps_df = dps_df.replace([np.inf, -np.inf], 0.0)

            # Drop input timeseries
            dps_df = dps_df.drop(columns=[count_node, good_node, status_node, planned_status_node])

            # OEE outputs are written back into the same per-site instance
            # space as their source time series.
            to_insert = [
                {
                    "instance_id": NodeId(space=source_space, external_id=external_id),
                    "datapoints": list(zip(dps_df[external_id].index, dps_df[external_id]))
                }
                for external_id in dps_df.columns
            ]

            try:
                client.time_series.data.insert_multiple(to_insert)
            except CogniteNotFoundError as e:
                # Create the missing oee timeseries since they don't exist
                ts_to_create = []
                for node_id in e.not_found:
                    print(f"Creating CogniteTimeSeries {node_id}")

                    external_id = node_id["instanceId"]["externalId"]

                    # change external_id to a readable name
                    # Ex: "OSLPROFILTRASYS185:off_spec" to "OSLPROFILTRASYS185 Off Spec"
                    name = external_id.split(":")
                    name[-1] = name[-1].replace("_", " ").title()

                    ts_to_create.append(
                        _build_calculated_time_series(
                            source_space=source_space,
                            external_id=external_id,
                            name=" ".join(name),
                            site_key=site_key,
                        )
                    )

                client.data_modeling.instances.apply(ts_to_create)
                client.time_series.data.insert_multiple(to_insert)
                