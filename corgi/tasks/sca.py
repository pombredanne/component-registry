import logging
import os
import re
import shutil
import subprocess  # nosec B404
import tarfile
from pathlib import Path
from typing import IO, Any, Optional, Tuple
from urllib.parse import urlparse

import requests
from celery_singleton import Singleton
from django.conf import settings
from requests import Response

from config.celery import app
from corgi.collectors.syft import Syft
from corgi.core.models import Component, ComponentNode, SoftwareBuild
from corgi.tasks.common import RETRY_KWARGS, RETRYABLE_ERRORS

LOOKASIDE_REGEX_SOURCE_PATTERNS = [
    # https://regex101.com/r/xYoHtX/1
    r"^(?P<hash>[a-f0-9]*)[ ]+(?P<file>[a-zA-Z0-9.\-_]*)",
    # https://regex101.com/r/mjtKif/1
    r"^(?P<alg>[A-Z0-9]*) \((?P<file>[a-zA-Z0-9.-]*)\) = (?P<hash>[a-f0-9]*)",
]
logger = logging.getLogger(__name__)


def save_component(component: dict[str, Any], parent: ComponentNode):
    meta = component.get("meta", {})
    if component["type"] not in Component.Type:
        logger.warning("Tried to save component with unknown type: ", component["type"])

    # Use all fields from Compoennt index and uniquness contrainst
    obj, created = Component.objects.get_or_create(
        type=component["type"],
        name=meta.pop("name", ""),
        version=meta.pop("version", ""),
        release="",
        arch="",
    )

    if "purl" in meta and obj.purl != meta["purl"]:
        logger.warning(
            "Saved component purl %s, does not match Syft purl: %s", obj.purl, meta["purl"]
        )

    node, _ = obj.cnodes.get_or_create(
        type=ComponentNode.ComponentNodeType.PROVIDES,
        parent=parent,
    )

    return created


@app.task(
    base=Singleton,
    autoretry_for=RETRYABLE_ERRORS,
    retry_kwargs=RETRY_KWARGS,
    soft_time_limit=settings.CELERY_LONGEST_SOFT_TIME_LIMIT,
)
def slow_software_composition_analysis(build_id: int):
    logger.info("Started software composition analysis for %s", build_id)
    software_build = SoftwareBuild.objects.get(build_id=build_id)

    try:
        # Get a matching SRPM first
        root_component = Component.objects.get(
            software_build=software_build, name=software_build.name, type=Component.Type.SRPM
        )
    except Component.DoesNotExist:
        # It might be a container image, else fail the task if neither matched
        root_component = Component.objects.get(
            software_build=software_build,
            name=software_build.name,
            type=Component.Type.CONTAINER_IMAGE,
            arch="noarch",
        )

    root_node = root_component.cnodes.first()
    if not root_node:
        raise ValueError(f"Didn't find root component node for {root_component.purl}")
    package_type = "rpms"
    if root_component.type == Component.Type.CONTAINER_IMAGE:
        package_type = "containers"
        _scan_remote_sources(root_component, root_node)

    distgit_sources = _get_distgit_sources(software_build.source, package_type)

    scan_files(root_node, distgit_sources)

    software_build.save_component_taxonomy()
    software_build.save_product_taxonomy()

    logger.info("Finished software composition analysis for %s", build_id)


def _scan_remote_sources(root_component, root_node):
    for upstream in root_component.upstreams:
        upstream_node = ComponentNode.objects.get(purl=upstream, parent=root_node)
        # This is potentially quite slow. We could probably make this more efficient by splitting
        # it off into another task
        if "remote_source_archive" in upstream_node.obj.meta_attr:
            # Download source to scratch
            remote_source_archive = upstream_node.obj.meta_attr["remote_source_archive"]
            url = urlparse(remote_source_archive)
            filename = url.path.rsplit("/", 1)[-1]
            target_filepath = Path(
                f"{settings.SCA_SCRATCH_DIR}/containers/{root_component.nvr}/{filename}"
            )
            # This scans the whole archive without unpacking it. If we find this returns extraneous
            # results we could probably unpack the archive and scan only the app subdirectory
            _download_source(remote_source_archive, target_filepath)
            # Scan source
            scan_files(upstream_node, [target_filepath])
            # remove source
            package_dir = Path(os.path.dirname(target_filepath))
            shutil.rmtree(package_dir)


def scan_files(anchor_node, sources):
    new_components = 0
    for component in Syft.scan_files(sources):
        if save_component(component, anchor_node):
            new_components += 1
    logger.info("Detected %s new components using Syft scan", new_components)


def _get_distgit_sources(source_url: str, package_type: str) -> list[Path]:
    distgit_sources: list[Path] = []
    raw_source, package_name = _archive_source(source_url, package_type)
    if not raw_source or not package_name:
        return []
    distgit_sources.append(raw_source)
    distgit_sources.extend(_download_lookaside_sources(raw_source, package_name, package_type))
    return distgit_sources


def _archive_source(source_url: str, package_type: str) -> Tuple[Optional[Path], str]:
    # (scheme, netloc, path, parameters, query, fragment)
    url = urlparse(source_url)

    # We only support git, git+https, git+ssh
    if not url.scheme.startswith("git"):
        raise ValueError("Cannot download raw source for anything but git protocol")

    git_remote = f"{url.scheme}://{url.netloc}{url.path}"
    package_name = url.path.rsplit("/", 1)[-1]

    target_path = Path(f"{settings.SCA_SCRATCH_DIR}/{package_type}/{package_name}")
    target_path.mkdir(exist_ok=True, parents=True)
    target_file = target_path / f"{url.fragment}.tar"
    if target_file.exists():
        # Perhaps another task is already processing, save cpu by bailing out
        return target_file, package_name

    logger.info("Fetching %s to %s", git_remote, target_file)
    _call_git_archive(git_remote, url.fragment, target_file)

    return target_file, package_name


def _call_git_archive(git_remote: str, url_fragment: str, target_file: Path) -> None:
    # for now git_remote and url.fragment are sanitized via url parsing and
    # coming from Brew.
    # We might consider more adding more sanitization if we accept ad-hoc source for scans
    git_archive_command = (
        "/usr/bin/git",
        "archive",
        "--format=tar",
        f"--remote={git_remote}",
        url_fragment,
    )
    with target_file.open("x") as target:
        subprocess.check_call(  # nosec B603
            git_archive_command,
            stdout=target,
        )


def _download_lookaside_sources(
    distgit_archive: Path, package_name: str, package_type: str
) -> list[Path]:
    source_tar = tarfile.open(distgit_archive)
    source_tarinfo = get_tarinfo(source_tar, "sources")
    if source_tarinfo is None:
        logger.warning("Didn't find sources file in %s", distgit_archive)
        return []

    lookaside_source: Optional[IO[bytes]] = source_tar.extractfile(source_tarinfo)
    if lookaside_source is None:
        logger.warning("Couldn't extract anything from %s", source_tarinfo)
        return []
    source_content = lookaside_source.readlines()

    lookaside_source_regexes = tuple(re.compile(p) for p in LOOKASIDE_REGEX_SOURCE_PATTERNS)
    downloaded_sources: list[Path] = []
    for line in source_content:
        match = None
        for regex in lookaside_source_regexes:
            match = regex.search(line.decode("UTF-8"))
            if match:
                break  # lookaside source regex loop
        if not match:
            continue  # source content loop
        lookaside_source_matches = match.groupdict()
        lookaside_source_filename = lookaside_source_matches.get("file")
        lookaside_source_checksum = lookaside_source_matches.get("hash")
        lookaside_hash_algorith = lookaside_source_matches.get("alg", "md5").lower()
        lookaside_path = (
            f"{package_type}/{package_name}/{lookaside_source_filename}/"
            f"{lookaside_hash_algorith}/{lookaside_source_checksum}/"
            f"{lookaside_source_filename}"
        )
        # https://<host>/repo/rpms/containernetworking-plugins/v0.8.6.tar.gz/md5/
        # 85eddf3d872418c1c9d990ab8562cc20/v0.8.6.tar.gz
        lookaside_download_url = f"{settings.LOOKASIDE_CACHE_BASE_URL}/{lookaside_path}"
        # eg. /tmp/rpms/containernetworking-plugins/v0.8.6.tar.gz/md5/
        # 85eddf3d872418c1c9d990ab8562cc20/v0.8.6.tar.gz
        target_filepath = Path(f"{settings.SCA_SCRATCH_DIR}/{lookaside_path}")
        if not target_filepath.exists():
            _download_source(lookaside_download_url, target_filepath)
        else:
            logger.info("%s already exists, not downloading", target_filepath)
        downloaded_sources.append(target_filepath)
    return downloaded_sources


def _download_source(download_url, target_filepath):
    package_dir = Path(os.path.dirname(target_filepath))
    package_dir.mkdir(exist_ok=True, parents=True)
    logger.info("Downloading sources from: %s", download_url)
    r: Response = requests.get(download_url)
    target_filepath.open("wb").write(r.content)


def get_tarinfo(members, archived_filename) -> Optional[tarfile.TarInfo]:
    for tarinfo in members:
        if tarinfo.name == archived_filename:
            return tarinfo
    return None
