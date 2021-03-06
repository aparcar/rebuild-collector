import email.parser
import io
import logging
import os
import re
import shutil
import tarfile
import tempfile
import urllib
from email.utils import getaddresses
from os import environ
from pathlib import Path
from time import sleep

import requests

from rbcollector.database import *

logger = logging.getLogger(__name__)

# https://github.com/archlinux/archweb/blob/a0ea44189ad4b7e8ed13b19a69173a0429a46208/devel/management/commands/reporead.py#L511
def parse_info(iofile):
    """
    Parses an Arch repo db information file, and returns variables as a list.
    """
    store = {}
    blockname = None
    for line in iofile:
        line = line.strip()
        if len(line) == 0:
            continue
        elif line.startswith("%") and line.endswith("%"):
            blockname = line[1:-1].lower()
            logger.debug("Parsing package block %s", blockname)
            store[blockname] = []
        elif blockname:
            store[blockname].append(line)
        else:
            raise Exception("Read package info outside a block: %s" % line)
    return store


def parse_repo(repopath):
    """
    Parses an Arch repo db file, and returns a list of RepoPackage objects.
    Arguments:
     repopath -- The path of a repository db file.
    """
    logger.info("Starting repo parsing")
    if not os.path.exists(repopath):
        logger.error("Could not read file %s", repopath)

    logger.info("Reading repo tarfile %s", repopath)
    filename = os.path.split(repopath)[1]
    m = re.match(r"^(.*)\.(db|files)\.tar(\..*)?$", filename)
    if m:
        reponame = m.group(1)
    else:
        logger.error("File does not have the proper extension")
        raise Exception("File does not have the proper extension")

    repodb = tarfile.open(repopath, "r")
    logger.debug("Starting package parsing")
    pkgs = {}
    for tarinfo in repodb.getmembers():
        if tarinfo.isreg():
            pkgid, fname = os.path.split(tarinfo.name)
            if fname in ("desc", "depends"):
                data_file = repodb.extractfile(tarinfo)
                data_file = io.TextIOWrapper(
                    io.BytesIO(data_file.read()), encoding="UTF-8"
                )
                try:
                    pkgs[pkgid] = parse_info(data_file)
                except UnicodeDecodeError:
                    logger.warning(
                        "Could not correctly decode %s, skipping file", tarinfo.name
                    )
                data_file.close()
                del data_file

            logger.debug("Done parsing file %s/%s", pkgid, fname)

    repodb.close()
    logger.info("Finished repo parsing, %d total packages", len(pkgs))
    return (reponame, pkgs)


def update_sources(config):
    print(f"Updating {config['name']}")

    origin = Origins.get(name=config["name"])

    for suite_name in config["suites"]:
        print(f"Updating {suite_name}")

        suite, _ = Suites.get_or_create(name=suite_name, origin=origin)
        component, _ = Components.get_or_create(name="packages", suite=suite)

        arch_independent_identifier = config.get("arch_independent_identifier")
        if arch_independent_identifier:
            target_independent, _ = Targets.get_or_create(
                name=arch_independent_identifier, component=component
            )

        for target_name in config.get("targets", []):
            target, _ = Targets.get_or_create(name=target_name, component=component)

            url = f"{config['uri']}/repos/last/{suite_name}/os/{target_name}/{suite_name}.db.tar.gz"
            req = requests.get(url)
            last_modified = datetime.strptime(
                req.headers.get("last-modified"), "%a, %d %b %Y %H:%M:%S %Z"
            )

            if target.timestamp >= last_modified:
                print(f"No source updates for {suite_name}/packages/{target.name}")
                continue

            with tempfile.TemporaryDirectory() as tmpdirname:
                Path(f"{tmpdirname}/{suite_name}.db.tar.gz").write_bytes(req.content)
                reponame, pkgs = parse_repo(f"{tmpdirname}/{suite_name}.db.tar.gz")

            for name, data in pkgs.items():
                if data["arch"][0] == arch_independent_identifier:
                    package_target = target_independent
                else:
                    package_target = target

                maintainers = getaddresses(data.get("packager", [""]))
                maintainer, _ = Maintainers.get_or_create(
                    email=maintainers[0][1], name=maintainers[0][0]
                )

                Sources.insert(
                    name=data["name"][0],
                    version=data["version"][0],
                    target=package_target,
                    cpe="",
                    maintainer=maintainer,
                    timestamp=last_modified,
                ).on_conflict(
                    conflict_target=[Sources.name, Sources.version, Sources.target],
                    update={
                        Sources.timestamp: last_modified,
                        Sources.maintainer: maintainer,
                    },
                ).execute()
            Targets.update(timestamp=last_modified).where(
                Targets.id == target
            ).execute()
            if target_independent:
                Targets.update(timestamp=last_modified).where(
                    Targets.id == target_independent
                ).execute()
