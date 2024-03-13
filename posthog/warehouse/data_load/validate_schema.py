from django.conf import settings

from posthog.warehouse.models import (
    get_latest_run_if_exists,
    get_or_create_datawarehouse_credential,
    DataWarehouseTable,
    DataWarehouseCredential,
    get_external_data_job,
    asave_datawarehousetable,
    acreate_datawarehousetable,
    asave_external_data_schema,
    get_table_by_schema_id,
    aget_schema_by_id,
)
from posthog.warehouse.models.external_data_job import ExternalDataJob
from posthog.temporal.common.logger import bind_temporal_worker_logger
from clickhouse_driver.errors import ServerException
from asgiref.sync import sync_to_async
from typing import Dict, Tuple
from posthog.utils import camel_to_snake_case


async def validate_schema(
    credential: DataWarehouseCredential, table_name: str, new_url_pattern: str, team_id: int
) -> Dict:
    params = {
        "credential": credential,
        "name": table_name,
        "format": "Parquet",
        "url_pattern": new_url_pattern,
        "team_id": team_id,
    }

    table = DataWarehouseTable(**params)
    table.columns = await sync_to_async(table.get_columns)(safe_expose_ch_error=False)

    return {
        "credential": credential,
        "name": table_name,
        "format": "Parquet",
        "url_pattern": new_url_pattern,
        "team_id": team_id,
    }


async def validate_schema_and_update_table(run_id: str, team_id: int, schemas: list[Tuple[str, str]]) -> None:
    """

    Validates the schemas of data that has been synced by external data job.
    If the schemas are valid, it creates or updates the DataWarehouseTable model with the new url pattern.

    Arguments:
        run_id: The id of the external data job
        team_id: The id of the team
        schemas: The list of schemas that have been synced by the external data job
    """

    logger = await bind_temporal_worker_logger(team_id=team_id)

    job: ExternalDataJob = await get_external_data_job(job_id=run_id)
    last_successful_job: ExternalDataJob | None = await get_latest_run_if_exists(job.team_id, job.pipeline_id)

    credential: DataWarehouseCredential = await get_or_create_datawarehouse_credential(
        team_id=job.team_id,
        access_key=settings.AIRBYTE_BUCKET_KEY,
        access_secret=settings.AIRBYTE_BUCKET_SECRET,
    )

    for _schema in schemas:
        _schema_id = _schema[0]
        _schema_name = _schema[1]

        table_name = f"{job.pipeline.prefix or ''}{job.pipeline.source_type}_{_schema_name}".lower()
        new_url_pattern = job.url_pattern_by_schema(camel_to_snake_case(_schema_name))

        # Check
        try:
            data = await validate_schema(
                credential=credential, table_name=table_name, new_url_pattern=new_url_pattern, team_id=team_id
            )
        except ServerException as err:
            if err.code == 636:
                logger.exception(
                    f"Data Warehouse: No data for schema {_schema_name} for external data job {job.pk}",
                    exc_info=err,
                )
            continue
        except Exception as e:
            # TODO: handle other exceptions here
            logger.exception(
                f"Data Warehouse: Could not validate schema for external data job {job.pk}",
                exc_info=e,
            )
            continue

        # create or update
        table_created = None
        if last_successful_job:
            try:
                table_created = await get_table_by_schema_id(_schema_id, team_id)
                if not table_created:
                    raise DataWarehouseTable.DoesNotExist
            except Exception:
                table_created = None
            else:
                table_created.url_pattern = new_url_pattern
                await asave_datawarehousetable(table_created)

        if not table_created:
            table_created = await acreate_datawarehousetable(external_data_source_id=job.pipeline.id, **data)

        # TODO: this should be async too
        table_created.columns = await sync_to_async(table_created.get_columns)()
        await asave_datawarehousetable(table_created)

        # schema could have been deleted by this point
        schema_model = await aget_schema_by_id(schema_id=_schema_id, team_id=job.team_id)

        if schema_model:
            schema_model.table = table_created
            schema_model.last_synced_at = job.created_at
            await asave_external_data_schema(schema_model)

    if last_successful_job:
        try:
            last_successful_job.delete_data_in_bucket()
        except Exception as e:
            logger.exception(
                f"Data Warehouse: Could not delete deprecated data source {last_successful_job.pk}",
                exc_info=e,
            )
            pass
