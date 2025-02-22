from typing import Iterable

from celery.utils.log import get_task_logger
from celery_singleton import Singleton

from config.celery import app
from corgi.collectors.rhel_compose import RhelCompose
from corgi.core.models import ProductComponentRelation, ProductStream, SoftwareBuild
from corgi.tasks.brew import slow_fetch_modular_build
from corgi.tasks.common import (
    BUILD_TYPE,
    RETRY_KWARGS,
    RETRYABLE_ERRORS,
    create_relations,
)

logger = get_task_logger(__name__)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def save_composes() -> None:
    logger.info("Setting up relations for all streams with composes")
    for stream in ProductStream.objects.exclude(composes__exact={}):
        save_compose.delay(stream.name)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def save_compose(stream_name) -> None:
    logger.info("Called save compose with %s", stream_name)
    ps = ProductStream.objects.get(name=stream_name)
    no_of_relations = 0
    for compose_url, variants in ps.composes.items():
        compose_id, compose_created_date, compose_data = RhelCompose.fetch_compose_data(
            compose_url, variants
        )
        for key in "srpms", "rhel_modules":
            if key not in compose_data:
                # Most composes don't have rhel_modules, in that case the rhel_modules
                # key won't exist so we can safely skip creating relations
                continue
            no_of_relations += create_relations(
                compose_data[key],
                BUILD_TYPE,
                compose_id,
                stream_name,
                ProductComponentRelation.Type.COMPOSE,
                slow_fetch_modular_build,
            )
    logger.info("Created %s new relations for stream %s", no_of_relations, stream_name)


@app.task(base=Singleton, autoretry_for=RETRYABLE_ERRORS, retry_kwargs=RETRY_KWARGS)
def get_builds(
    compose_names: Iterable[str] = (), stream_name: str = "", force_process: bool = False
) -> int:
    """Get compose build IDs, optionally for only a particular stream or set of composes"""
    # We exclude CENTOS build_types because the only product stream (openstack-rdo) stored in
    # CENTOS koji doesn't use modules, and we call slow_fetch_modular_build below
    relations_query = ProductComponentRelation.objects.filter(
        type=ProductComponentRelation.Type.COMPOSE, software_build=None
    ).exclude(build_type=SoftwareBuild.Type.CENTOS)
    if compose_names:
        relations_query = relations_query.filter(external_system_id__in=compose_names)
    elif stream_name:
        relations_query = relations_query.filter(product_ref=stream_name)

    processed_builds = 0
    for build_id in relations_query.values_list("build_id", flat=True).distinct().iterator():
        logger.info(f"Processing Compose relation build with id: {build_id}")
        slow_fetch_modular_build.delay(build_id, force_process=force_process)
        processed_builds += 1
    return processed_builds
