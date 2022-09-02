# Copyright (c) 2021, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import random
import shutil
import tarfile
import os
import time

from jinja2 import Template
import ast

import dns
import dns.rdtypes.ANY.CNAME
import dns.rdtypes.ANY.NS
import dns.rdtypes.ANY.MX
import dns.rdtypes.ANY.SOA
import dns.rdtypes.IN.A
import dns.rdtypes.IN.AAAA
import dns.rdtypes.ANY.TXT
import dns.rdtypes.ANY.PTR
import dns.rdtypes.IN.SRV


# todo: use configuration for the logger
from pyaml_env import parse_config

from util.helpers import (get_logger, get_config_yml_path,
                          path_to_input, path_to_output,
                          path_to_containers)

logger = get_logger(__name__)


def rdata_and_type_for_txt(value):
    rdtype = dns.rdtypes.ANY.TXT.TXT
    rdatatype = dns.rdatatype.TXT
    if len(value) > 250:
        rdata = rdtype(dns.rdataclass.IN, rdatatype, strings=(value[:250], value[250:]))
    else:
        rdata = rdtype(dns.rdataclass.IN, rdatatype, strings=(value,))
    return rdata, rdatatype


def rdata_and_type_for_a(value):
    rdtype = dns.rdtypes.IN.A.A
    rdatatype = dns.rdatatype.A
    rdata = rdtype(dns.rdataclass.IN, rdatatype, address=value)
    return rdata, rdatatype


def rdata_and_type_for_aaaa(value):
    rdtype = dns.rdtypes.IN.AAAA.AAAA
    rdatatype = dns.rdatatype.AAAA
    rdata = rdtype(dns.rdataclass.IN, rdatatype, address=value)
    return rdata, rdatatype


def rdata_and_type_for_mx(value):
    rdtype = dns.rdtypes.ANY.MX.MX
    rdatatype = dns.rdatatype.MX
    preference = value[0]
    exchange = value[1]
    rdata = rdtype(
            dns.rdataclass.IN,
            rdatatype,
            preference=preference,
            exchange=dns.name.from_text(exchange)
    )
    return rdata, rdatatype


def rdata_and_type_for_cname(value):
    rdtype = dns.rdtypes.ANY.CNAME.CNAME
    rdatatype = dns.rdatatype.CNAME
    rdata = rdtype(dns.rdataclass.IN, rdatatype, target=dns.name.from_text(value))
    return rdata, rdatatype


def rdata_and_type_for_srv(value):
    rdtype = dns.rdtypes.IN.SRV.SRV
    rdatatype = dns.rdatatype.SRV
    priority = value[0]
    weight = value[1]
    port = value[2]
    target = value[3]
    rdata = rdtype(
        dns.rdataclass.IN,
        rdatatype,
        priority=priority,
        weight=weight,
        port=port,
        target=dns.name.from_text(target)
    )
    return rdata, rdatatype


def rdata_and_type_for_ns(value):
    rdtype = dns.rdtypes.ANY.NS.NS
    rdatatype = dns.rdatatype.NS
    rdata = rdtype(
        dns.rdataclass.IN,
        rdatatype,
        target=dns.name.from_text(value)
    )
    return rdata, rdatatype


def rdata_and_type_for_record(type, value):
    # the literal_eval is something to do with long values showing up as
    # adjacent strings "like" "this"
    if type == "TXT":
        return rdata_and_type_for_txt(ast.literal_eval(value).encode())
    elif type == "A":
        return rdata_and_type_for_a(value)
    elif type == "AAAA":
        return rdata_and_type_for_aaaa(value)
    elif type == "MX":
        return rdata_and_type_for_mx(value)
    elif type == "CNAME":
        return rdata_and_type_for_cname(value)
    elif type == "SRV":
        return rdata_and_type_for_srv(value)
    elif type == "NS":
        return rdata_and_type_for_ns(value)
    else:
        raise Exception(f"unsupported record type {type}")


def rdataset_for_soa(origin_node):
    return origin_node.find_rdataset(
        dns.rdataclass.IN,
        rdtype=dns.rdatatype.SOA,
        create=True
    )


def rdata_for_soa(mname, rname, serial, refresh, retry, expire, minimum):
    return dns.rdtypes.ANY.SOA.SOA(
        dns.rdataclass.IN,
        dns.rdatatype.SOA,
        mname=dns.name.from_text(mname),
        rname=dns.name.from_text(rname),
        serial=serial,
        refresh=refresh,
        retry=retry,
        expire=expire,
        minimum=minimum
    )


def rdataset_for_sub_zone_and_rdatatype(zone, sub_zone, rdatatype):
    return zone.find_rdataset(
        sub_zone,
        rdtype=rdatatype,
        create=True
    )


def add_record_rel(zone, site_name, sub_zone, type, value):
    rel_zone = relativize_name(site_name, sub_zone)
    add_record_norel(zone, rel_zone, type, value)


def add_record_norel(zone, sub_zone, type, value):
    rdata, rdatatype = rdata_and_type_for_record(type, value)
    rdataset = rdataset_for_sub_zone_and_rdatatype(zone, sub_zone, rdatatype)
    rdataset.add(rdata, ttl=300)


def add_soa(zone, mname, rname, serial, refresh, retry, expire, minimum):
    origin_node = zone.find_node("@", create=True)

    soa_rdataset = rdataset_for_soa(origin_node)
    rdata = rdata_for_soa(
            mname=mname,
            rname=rname,
            serial=serial,
            refresh=refresh,
            retry=retry,
            expire=expire,
            minimum=minimum
    )
    soa_rdataset.add(rdata, 300)


def get_serial():
    return int(time.time())


def site_to_zone(config, site_name, site, all_sites):
    zone = dns.zone.Zone(origin=dns.name.from_text(site['public_domain']))

    acme_ns = f"acme.{config['system_root_zone']}"

    add_soa(
        zone,
        mname=config['dns']['soa_nameserver'],
        rname=config['dns']['soa_mailbox'],
        serial=get_serial(),  # this forces AXFR transfer
        refresh=300,
        retry=300,
        expire=1209600,
        minimum=300
    )

    for default_ns in config['dns']['default_ns']:
        add_record_rel(zone, site_name, site_name, "NS", default_ns)

    def is_additional_domain(alt_name):
        for prefix in site['additional_domain_prefix']:
            if alt_name.startswith(f"{prefix}."):
                return True

    for alt_name in sorted(set(site["server_names"])):
        # this somehow lets bind9 forward these requests to certbot's dns-helper
        add_record_rel(zone, site_name, f"_acme-challenge.{alt_name}", "NS", acme_ns)

        # we don't want _acme-challenge.www
        # also no A record for www and additional_domain_prefix, just CNAME
        if alt_name.startswith("www.") or is_additional_domain(alt_name):
            logger.debug(f"www/additional_domain handling for {alt_name}")
            add_record_rel(zone, site_name, alt_name, "CNAME", site_name)
        else:
            # it's a bit kludgy, but we say the controller is part of dnet "controller"
            # so we can specify that kibana, elasticsearch, and doh-proxy live there.
            for edge in [config['controller']] + config['edges']:
                if edge['dnet'] == site['dnet']:
                    add_record_rel(zone, site_name, alt_name, "A", edge['ip'])

    for rel_zone, type_and_values in site['dns_records'].items():
        for type_and_value in type_and_values:
            if type_and_value['type'] == 'CNAME':
                """
                check if its an individual site
                and add _acme challenge for it because putting _acme-challenge
                record in its individual zone file won't be AXFR tranfer to
                easydns as we only register the root domain
                """
                if f"{rel_zone}.{site_name}" in all_sites:
                    logger.debug(f"{rel_zone}.{site_name} "
                                  "is an individual site in all_sites, adding _acme-challenge")
                    add_record_rel(zone, site_name, f"_acme-challenge.{rel_zone}.{site_name}", "NS", acme_ns)
                    add_record_rel(zone, site_name, f"_acme-challenge.www.{rel_zone}.{site_name}", "NS", acme_ns)
            add_record_norel(zone, rel_zone, type_and_value['type'], type_and_value['value'])

    return zone


def get_output_filename(sites_dir, name):
    return os.path.join(sites_dir, f"{name}.zone")


def get_etc_bind_filename(name):
    return os.path.join("/etc/bind/deflect", f"{name}.zone")


def template_named_conf(config, client_and_system_sites):
    named_conf_string = """view "primary" {

    // Default no, but will toggle it during certbot challenge
    recursion no;
"""

    named_conf_string += """
    // start system_root_zone
    """
    zone_ = config['system_root_zone']
    named_conf_string += zone_block_root(
            zone_,
            indent=" "*4,
            config=config
    )
    named_conf_string += """
    // end system_root_zone
    """

    named_conf_acme = ''
    for site in sorted(client_and_system_sites.values(), key=lambda s: s['public_domain']):
        public_domain_ = site['public_domain']
        if public_domain_ != zone_:
            named_conf_string += zone_block_root(public_domain_, indent=" " * 4, config=config)
            for server_name in sorted(set(site['server_names'])):
                named_conf_acme += zone_block_acme_challenge(server_name, indent=" "*4)

    named_conf_string += named_conf_acme + "};\n"

    return named_conf_string


def template_controller_zone(in_filename, out_filename, config):
    with open(in_filename, "r") as tf:
        template = Template(tf.read())
        with open(out_filename, "w") as zf:
            base_zone = template.render(
                serial=get_serial(),  # this forces AXFR transfer
                ip=config['controller']['ip'],
                soa_mailbox=config['dns']['soa_mailbox'],
                soa_nameserver=config['dns']['soa_nameserver'],
                default_ns=config['dns']['default_ns'],
                root_domain=config['system_root_zone'],
                edges=random.sample(config['edges'], 6 if len(config['edges']) > 6 else len(config['edges']))
            )
            # add some extra stuff to the root zone
            # like edges in config, and other neceseary stuff
            for record in config['root_zone_extra']:
                for rr in config['root_zone_extra'][record]:
                    base_zone += zone_block_root_zone_record(
                        record,
                        rr['type'],
                        rr['value'])

            base_zone += "\n\n; auto populate controller and edges records"
            if 'controller' not in config['root_zone_extra']:
                base_zone += zone_block_root_zone_record(
                    extract_subdomain(config['controller']['hostname']),
                    'A',
                    config['controller']['ip'])

            for edge in config['edges']:
                edge_name = extract_subdomain(edge['hostname'])
                if edge_name not in config['root_zone_extra']:
                    base_zone += zone_block_root_zone_record(
                        extract_subdomain(edge['hostname']),
                        'A',
                        edge['ip'])

            zf.write(base_zone + "\n")  # trailing newline at end of file


def extract_subdomain(hostname):
    return hostname.split('.')[0]


def zone_block_root_zone_record(host, type, ip):
    template = Template("""
{{ host }}     IN      {{ type }}       {{ ip }}
""")
    return template.render(host=host, type=type, ip=ip)


# "example.com" -> "@"
# "www.example.com" -> "www"
def relativize_name(canonical_name, alt_name):
    if alt_name == canonical_name:
        return "@"
    else:
        return alt_name.replace("." + canonical_name, "")


def zone_block_root(domain, indent="", config=None):
    filename = get_etc_bind_filename(domain)
    template = Template("""
{{indent}}zone "{{domain}}" {
{{indent}}    type master;
{{indent}}    file "{{filename}}";
{{indent}}    also-notify { {{also_notify}} };
{{indent}}    allow-query { {{allow_query}} };
{{indent}}    allow-transfer { {{allow_transfer}} };
{{indent}}};

""")
    return template.render(domain=domain,
                           filename=filename,
                           also_notify=config['dns']['also-notify'],
                           allow_query=config['dns']['allow-query'],
                           allow_transfer=config['dns']['allow-transfer'],
                           indent=indent)


def zone_block_acme_challenge(domain, indent=""):
    template = Template("""
{{indent}}zone "_acme-challenge.{{domain}}" {
{{indent}}    type forward;
{{indent}}    forward only;
{{indent}}    forwarders { 127.0.0.1 port 5053; };
{{indent}}};

""")
    return template.render(domain=domain, indent=indent)


def generate_bind_config(config, all_sites, timestamp):
    """
    Create all the zone files
    """
    if not hasattr(dns, 'zone'):
        logger.warn(
            'HACKY: Something is wrong here and dns doesn\'t have zone'
        )
        from dns.zone import Zone

    output_dir = f"{path_to_output()}/{timestamp}/etc-bind"
    output_dir_tar = f"{output_dir}.tar"
    if len(output_dir) == 0:  # TODO: fixme did we mean to check for something else here?
        raise Exception("output_dir cannot be empty")
    if os.path.isdir(output_dir):
        logger.debug(f'Removing output dir: {output_dir}')
        # XXX making extra sure this is a local dir?
        shutil.rmtree(f"{output_dir}")
    os.mkdir(output_dir)
    sites_dir = os.path.join(output_dir, "deflect")
    os.mkdir(sites_dir)

    # XXX fix this pattern
    client_and_system_sites = {**all_sites['client'], **all_sites['system']}

    # this config file points at all the zone files
    named_conf = template_named_conf(config, client_and_system_sites)
    with open(os.path.join(output_dir, "named.conf.local"), "w") as f:
        f.write(named_conf)

    # XXX using a jinja template here, but using dnspython for everything else
    in_filename = f"{path_to_input()}/templates/controller.zone.j2"
    out_filename = get_output_filename(sites_dir, config['system_root_zone'])
    template_controller_zone(in_filename, out_filename, config)

    # write out a zone file for each site
    for site_name, site in client_and_system_sites.items():
        zone = site_to_zone(config, site_name, site, all_sites=client_and_system_sites)
        zone.to_file(get_output_filename(sites_dir, site_name), relativize=True, sorted=True)

    # template for edgemanage
    from distutils.dir_util import copy_tree
    zone_template_dir = os.path.join(output_dir, "deflect_zones")
    # We do copy here because we still want a working zone file
    # in /etc/bind/deflect initially for the bind server to work
    # later edgemanage will take over and overwrite it
    copy_tree(sites_dir, zone_template_dir)
    logger.info(f"Copy zone files to {zone_template_dir}")

    # move zone into dnet sub-folder
    for hostname, site in all_sites['client'] .items():
        dnet = site['dnet']
        if not os.path.isdir(os.path.join(zone_template_dir, dnet)):
            os.mkdir(os.path.join(zone_template_dir, dnet))

        remove_soa_ns_a_record(
            os.path.join(zone_template_dir, f"{hostname}.zone"),
            os.path.join(zone_template_dir, dnet, f"{hostname}.zone"),
            hostname, dnet)

        os.unlink(os.path.join(zone_template_dir, f"{hostname}.zone"))

    # copy dns config files to output dir
    dns_configs = [
        'rndc.key',
        'rndc.conf'
    ]
    for dns_config in dns_configs:
        logger.info(f"Copy {dns_config} to output dir")
        shutil.copyfile(
            f"{path_to_input()}/config/{dns_config}",
            f"{output_dir}/{dns_config}")

    # copy named-checks.sh
    # copy these file since /etc/bind is a volume, as COPY in dockerfile won't work
    dns_configs_in_container = [
        'named-checks.sh',
        'named.conf',
        'named.conf.default-zones',
        'named.conf.options',
    ]
    for dns_config in dns_configs_in_container:
        logger.info(f"Copy {dns_config} to output dir")
        shutil.copyfile(
            f"{path_to_containers()}/bind/{dns_config}",
            f"{output_dir}/{dns_config}")

    if os.path.isfile(output_dir_tar):
        logger.debug("Removing old output file: %s", output_dir_tar)
        os.remove(output_dir_tar)

    logger.info(f'Writing {output_dir_tar}')
    with tarfile.open(output_dir_tar, "x") as tar:
        tar.add(output_dir, arcname=".")


def remove_soa_ns_a_record(old_path, new_path, hostname, dnet):
    """
    Remove SOA/NS/A record so edgemanage could manage those record
    """
    zone = dns.zone.from_file(old_path, origin=f"{hostname}.")
    zone.delete_rdataset('@', dns.rdatatype.A)
    zone.delete_rdataset('@', dns.rdatatype.NS)
    zone.delete_rdataset('@', dns.rdatatype.SOA)
    zone.to_file(new_path, relativize=True, sorted=True)
    logger.debug(f"Removed SOA/NS/A record for {dnet}/{hostname}.zone")


if __name__ == "__main__":
    from orchestration.shared import get_all_sites

    config = parse_config(get_config_yml_path())

    all_sites, formatted_time = get_all_sites()

    generate_bind_config(config, all_sites, formatted_time)
