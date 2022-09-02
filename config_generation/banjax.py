# Copyright (c) 2021, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import copy
import yaml
import os
import tarfile
import shutil
import base64


# todo: use configuration for the logger
from pyaml_env import parse_config

from util.helpers import get_logger, PEMS, LIST_NAME_TO_DECISION, \
    get_config_yml_path, get_banjax_config_yml_path, path_to_input, path_to_output

logger = get_logger(__name__)


def site_decision_lists(site):
    """
    Get allow, block and challenge lists
    """
    decision_lists = {}
    for list_name, decision in LIST_NAME_TO_DECISION.items():
        if site.get(list_name):
            decision_lists[decision] = site[list_name]
    return decision_lists


# doesn't read well. "block" or "no_block" means what to do after an IP
# fails a bunch of challenges.
def sitewide_sha_inv(site):
    if site.get('sitewide_sha_inv'):
        return "block"
    elif site.get('sitewide_sha_inv_no_block'):
        return "no_block"
    else:
        return None


def generate_banjax_config(config, all_sites, formatted_time):
    # TODO: refactor into smaller functions

    banjax_next_config = parse_config(get_banjax_config_yml_path())
    banjax_next_config["config_version"] = formatted_time

    client_sites = all_sites['client']  # XXX most places never care about system sites

    # todo: same independent iteration, merge it
    all_site_decision_lists = {}
    for _, site in client_sites.items():
        decision_lists = site_decision_lists(site)
        if decision_lists != {}:
            all_site_decision_lists[site['public_domain']] = decision_lists
    banjax_next_config["per_site_decision_lists"] = all_site_decision_lists

    # example.com: challenge and block
    # foobar.com:  challenge and do nothing
    # asdf.com:    None
    sitewide_sha_inv_dict = {}
    for _, site in client_sites.items():
        fail_action = sitewide_sha_inv(site)
        if fail_action:
            sitewide_sha_inv_dict[site['public_domain']] = fail_action
    banjax_next_config["sitewide_sha_inv_list"] = sitewide_sha_inv_dict

    all_site_password_protected_paths = {}
    all_site_password_protected_path_exceptions = {}
    all_site_password_hashes = {}
    for _, site in client_sites.items():
        paths = site.get('password_protected_paths', [])
        exception_paths = site.get('password_protected_path_exceptions', [])
        password_b64 = site.get('password_protected_paths_password', None)
        if len(paths) > 0:
            if password_b64 is None:
                raise Exception("missing password_protected_paths_password!!!")
            all_site_password_protected_paths[site['public_domain']] = paths
            all_site_password_protected_path_exceptions[site['public_domain']] = exception_paths
            # banjax expects to read something generated like:
            # python3 -c "import hashlib; print(hashlib.sha256('password'.encode()).hexdigest())"
            password_bytes = base64.b64decode(password_b64)
            password_hex = password_bytes.hex()
            all_site_password_hashes[site['public_domain']] = password_hex
    banjax_next_config["password_protected_paths"] = all_site_password_protected_paths
    banjax_next_config["password_protected_path_exceptions"] = all_site_password_protected_path_exceptions
    banjax_next_config["password_hashes"] = all_site_password_hashes

    # todo: same independent iteration, merge it
    all_per_site_rate_limited_regexes = {}
    for _, site in client_sites.items():
        rate_limited_regexes = site.get('rate_limited_regexes', [])
        if len(rate_limited_regexes) > 0:
            all_per_site_rate_limited_regexes[site['public_domain']] = rate_limited_regexes
            # XXX append per site regex to global regex so it works
            # as we don't have a per site regex implemented in banjax
            banjax_next_config["regexes_with_rates"] += copy.deepcopy(rate_limited_regexes)
    banjax_next_config["per_site_rate_limited_regexes"] = all_per_site_rate_limited_regexes

    # pass disable_logging = true sites to banjax
    banjax_next_config["disable_logging"] = {}
    for domain, site in client_sites.items():
        if site['disable_logging']:
            banjax_next_config["disable_logging"][domain] = True

    # todo: refactor into a writing/ output function - this pattern is repeated with a few variations
    output_dir = f"{path_to_output()}/{formatted_time}/etc-banjax"
    if os.path.isdir(output_dir):
        # XXX making extra sure this is a local dir?
        shutil.rmtree(f"{output_dir}")
    os.mkdir(output_dir)
    with open(f"{output_dir}/banjax-config.yaml", "w") as f:
        f.write(yaml.dump(banjax_next_config, default_flow_style=False))

    try:
        for each in PEMS:
            input_banjax_pem_ = f"{path_to_input()}/banjax/{each}"
            logger.info(f'Copying PEMS: {input_banjax_pem_}')
            shutil.copy(input_banjax_pem_, output_dir)
    except FileNotFoundError:
        logger.error('Missing Kafka pems, ignore if you don\'t need the '
                     'connection to Baskerville / Kafka')

    if os.path.isfile(f"{output_dir}.tar"):
        os.remove(f"{output_dir}.tar")

    logger.info(f"Writing {output_dir}.tar")
    with tarfile.open(f"{output_dir}.tar", "x") as tar:
        tar.add(output_dir, arcname=".")


if __name__ == "__main__":
    from orchestration.shared import get_all_sites

    config = parse_config(get_config_yml_path())

    all_sites, formatted_time = get_all_sites()

    generate_banjax_config(config, all_sites, formatted_time)
