# Copyright (c) 2021, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
import shutil
import logging
import tarfile
import yaml

from pyaml_env import parse_config
from util.config_parser import parse_with_defaults
from util.helpers import (
    get_logger,
    get_config_yml_path,
    path_to_input,
    path_to_output,
)

# todo: use configuration for the logger
logger = get_logger(__name__, logging_level=logging.DEBUG)


def generate_edgemanage_config(config, all_sites, timestamp):
    # Setup dirs
    output_dir = f"{path_to_output()}/{timestamp}/etc-edgemanage"
    output_dir_tar = f"{output_dir}.tar"

    if len(output_dir) == 0:
        raise Exception("output_dir cannot be empty")

    if os.path.isdir(output_dir):
        logger.debug(f"Removing output dir: {output_dir}")
        shutil.rmtree(f"{output_dir}")

    os.mkdir(output_dir)

    # Process config file
    edgemanage_config = parse_with_defaults("edgemanage", "edgemanage.yaml")

    # Enforce the path where prometheus data will be stored so it aligns with
    # the structure of the rest of the services
    edgemanage_config["prometheus_logs"] = os.path.join(
        config["prometheus_data"]["container_path"], ""
    )

    # Generate edges list
    edges_dir = os.path.join(output_dir, "edges")
    os.mkdir(edges_dir)

    # Generate edges in dnet files
    dnet_to_edges = {}
    for edge in config["edges"]:
        if edge["dnet"] in dnet_to_edges:
            dnet_to_edges[edge["dnet"]].append(edge["hostname"])
        else:
            dnet_to_edges[edge["dnet"]] = [edge["hostname"]]

    for dnet in dnet_to_edges:
        with open(os.path.join(edges_dir, dnet), "w") as f:
            f.writelines(f"# Generated by deflect-next @{timestamp}\n")
            f.writelines("%s\n" % l for l in dnet_to_edges[dnet])

    with open(f"{output_dir}/edgemanage.yaml", "w") as f:
        f.write(yaml.dump(edgemanage_config, default_flow_style=False))

    # Copy the test object to the container
    testobject_name = "myobject.edgemanage"
    shutil.copyfile(
            f"{path_to_input()}/config/{testobject_name}",
            f"{output_dir}/{testobject_name}")
    edgemanage_config["testobject"]["local"] = "/etc/edgemanage/{testobject_name}"

    # Output files, compress and clean
    if os.path.isfile(output_dir_tar):
        logger.debug(f"Removing {output_dir_tar}")
        os.remove(output_dir_tar)

    logger.debug(f"Writing {output_dir_tar}")
    with tarfile.open(output_dir_tar, "x") as tar:
        tar.add(output_dir, arcname=".")


if __name__ == "__main__":
    from orchestration.shared import get_all_sites

    config = parse_config(get_config_yml_path())
    all_sites, formatted_time = get_all_sites()
    generate_edgemanage_config(config, all_sites, formatted_time)
