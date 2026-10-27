from datetime import datetime, timedelta, timezone
from timeit import default_timer

from cognite.client import CogniteClient
from cognite.client.data_classes import ExtractionPipelineRun
from cognite.client.data_classes.data_modeling import NodeId
from cognite.client.data_classes.data_modeling.cdm.v1 import CogniteTimeSeries

from ice_cream_factory_api import IceCreamFactoryAPI

from cognite.client.config import global_config
global_config.disable_pypi_version_check = True


def get_site_space(site: str) -> str:
    """Each site's instances (ISAAsset, ISATimeSeries, etc.) live in their own instance space."""
    return f"{site.lower()}_sp"


def get_time_series_for_site(client: CogniteClient, site):
    site_space = get_site_space(site)

    # ISATimeSeries implements CogniteTimeSeries, so its data lives in the same
    # cdf_cdm:CogniteTimeSeries container. List through the core cdf_cdm view (not the
    # custom ISATimeSeries view) - this still returns ISATimeSeries nodes, and it's required
    # for the generated CogniteTimeSeries class to deserialize the response correctly.
    # Each site's instances live in their own space, so the space is the site filter.
    time_series = list(client.data_modeling.instances.list(
        instance_type=CogniteTimeSeries,
        space=site_space,
        limit=None
    ))
    print(f"  [checkpoint] ISATimeSeries retrieved for {site} (space={site_space}): {len(time_series)} time series (before name filter)")

    if not time_series:
        print(
            f"----No ISATimeSeries in CDF for {site}!----\n"
            f"    Check that the time series have been created in space {site_space}"
        )
        return []

    time_series = [
        item for item in time_series
        if any(substring in item.external_id for substring in ["planned_status", "good"])
    ]
    print(f"  [checkpoint] ISATimeSeries after 'planned_status'/'good' filter for {site}: {len(time_series)} time series")

    return time_series


def report_ext_pipe(client: CogniteClient, status, message=None):
    ext_pipe_run = ExtractionPipelineRun(
        extpipe_external_id="ep_icapi_datapoints",
        status=status,
        message=message
    )

    client.extraction_pipelines.runs.create(run=ext_pipe_run)

def handle(client: CogniteClient = None, data=None):
    report_ext_pipe(client, "seen")
    
    sites = None
    backfill = None
    hours = None
    max_hours = 336

    if data:
        sites = data.get("sites")
        backfill = data.get("backfill")
        hours = data.get("hours")

        if hours and hours > max_hours:
            print(f"{hours} > {max_hours}! The Ice Cream API can't serve more than {max_hours} hours of datapoints, setting hours to max")
            hours = max_hours

    all_sites = [
        "Houston",
        "Oslo",
        "Kuala_Lumpur",
        "Hannover",
        "Nuremberg",
        "Marseille",
        "Sao_Paulo",
        "Chicago",
        "Rotterdam",
        "London",
    ]

    sites = sites or all_sites
    backfill = backfill or True
    hours = hours or max_hours

    now = datetime.now(timezone.utc).timestamp() * 1000
    increment = timedelta(hours=hours).total_seconds() * 1000

    ice_cream_api = IceCreamFactoryAPI(base_url="https://ice-cream-factory.inso-internal.cognite.ai")

    try:
        for site in sites:
            print(f"Getting Data Points for {site}")
            big_start = default_timer()

            site_space = get_site_space(site)
            time_series = get_time_series_for_site(client, site)
            print(f"  [checkpoint] Time series to process for {site}: {len(time_series)}")

            if not backfill:
                latest_retrieved = client.time_series.data.retrieve_latest(
                    external_id=[ts.external_id for ts in time_series],
                    ignore_unknown_ids=True
                )
                print(f"  [checkpoint] Latest datapoints retrieved for {site}: {len(latest_retrieved)} time series with existing data")
                latest_dps = {dp.external_id: dp.timestamp for dp in latest_retrieved}
            else:
                latest_dps = None

            to_insert = []
            skipped_series = []
            for ts in time_series:
                # figure out the window of datapoints to pull for this Time Series
                latest = latest_dps[ts.external_id][0] if not backfill and latest_dps.get(ts.external_id) else None

                start = latest if latest else now - increment
                end = now

                try:
                    dps_list = ice_cream_api.get_datapoints(timeseries_ext_id=ts.external_id, start=start, end=end)
                except Exception as e:
                    print(f"    [checkpoint] SKIPPING {ts.external_id} after retries exhausted: {type(e).__name__}: {e}")
                    skipped_series.append(ts.external_id)
                    continue

                total_dps = sum(len(item["datapoints"]) for item in dps_list)
                print(f"    [checkpoint] {ts.external_id}: {len(dps_list)} related time series, {total_dps} datapoints retrieved from Ice Cream Factory API")

                for dp_dict in dps_list:
                    dp_dict["instance_id"] = NodeId(space=site_space, external_id=dp_dict["instance_id"])

                to_insert.extend(dps_list)

                if len(to_insert) > 50:
                    print(f"    [checkpoint] Inserting batch of {len(to_insert)} time series' datapoints into {site_space}")
                    client.time_series.data.insert_multiple(datapoints=to_insert)
                    to_insert = []

            if skipped_series:
                print(f"  [checkpoint] {len(skipped_series)} time series skipped for {site} due to persistent API errors: {skipped_series}")

            if to_insert:
                print(f"  [checkpoint] Inserting final batch of {len(to_insert)} time series' datapoints into {site_space}")
                client.time_series.data.insert_multiple(datapoints=to_insert)
                print(f"  {hours}h of Datapoints took {default_timer() - big_start:.2f} seconds")
            else:
                print(f"  No TimeSeries, for {hours}h of Datapoints took {default_timer() - big_start:.2f} seconds")

        report_ext_pipe(client, "success")
    except Exception as e:
        print(f"[checkpoint] FAILED: {type(e).__name__}: {e}")
        report_ext_pipe(client, "failure", str(e))