# Copyright (c) 2021, eQualit.ie inc.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import json
import logging
import time
import docker
import yaml
from datetime import datetime

from orchestration import old_to_new_site_dict
from orchestration.helpers import get_logger, NAME_TO_ROLE, \
    orchestration_path, FILENAMES_TO_TAIL, DEFAULT_RESTART_POLICY, \
    get_sites_yml_path, get_persisted_config_yml_path

# todo: use configuration for the logger
logger = get_logger(__name__, logging_level=logging.DEBUG)


def find_existing_or_start_new_container(client, image_name, image_build_timestamp, config):
    """
    Find a container based on image_name
    If one does not exist, kill any containers with image_name
    and build a new one
    """
    logger.info(f"find_existing_or_start_new ({image_name})")
    containers = client.containers.list(
        filters={"label": f"name={image_name}"}
    )

    if len(containers) == 1:
        logger.debug(f"find_existing_or_start_new ({image_name}), found 1")
        return containers[0]
    if len(containers) == 0:
        logger.debug(f"find_existing_or_start_new ({image_name}), found 0")
        kill_containers_with_label(client, image_name)
        (new_image, new_image_logs) = build_new_image(
            image_name, client, image_build_timestamp)
        # XXX not sure what behavior we want with the timestamp here, actually
        new_container = start_new_container(
            client, image_name, new_image, image_build_timestamp, config)
        return new_container

    # TODO:@joe I commented this out since it was for debugging right?
    # import pdb
    # pdb.set_trace()
    raise Exception(
        f"expected 0 or 1 container called {image_name}, but found {len(containers)}")


def find_running_container(client, image_name):
    """
    Returns a Docker container if one exists with the given name (image_name)
    otherwise raises RuntimeError
    """
    containers = client.containers.list(
        filters={"name": image_name}
    )

    if len(containers) != 1:
        msg = f"expected 1 container ({image_name}), " \
              f"found {len(containers)}. bailing out!"
        logger.error(msg)
        raise RuntimeError(msg)

    return containers[0]


def get_image_name(name, timestamp, registry=''):
    if registry:
        registry += ':'
    # user/registry:tag
    return f"{registry}deflect-next-{name}-{timestamp}",


def build_new_image(name, client, timestamp, registry=''):
    """
    Builds a new image based on the image name,
    returns image and respective logs
    """
    # XXX ugh
    role = {
        "nginx": "edge",
        "bind-server": "controller",
        "certbot": "controller",
        "banjax-next": "edge",
        "origin-server": "",
        "doh-proxy": "testing",
        "filebeat": "",
        "legacy-filebeat": "",
        "legacy-logstash": "",
        "elasticsearch": "",
        "kibana": "",
        "metricbeat": "",
        "pebble": "testing",
    }[name]

    logger.info(f'Building for {name}: {role}')

    (image, image_logs) = client.images.build(
        path=f"{orchestration_path()}/../containers/{role}/{name}",
        tag=get_image_name(name, timestamp, registry),
        rm=True     # remove intermediate containers to avoid system pollution
    )
    return image, image_logs


def start_new_nginx_container(client, image_id, timestamp):
    """
    Using the same volume: runs as many (tailer) containers as the
    FILENAMES_TO_TAIL and eventually starts a new nginx container
    """
    logs_volume = client.volumes.create(name=f"nginx-{timestamp}")
    # XXX think about this approach. also need to delete these containers...
    # TODO: delete? when?
    for filename_to_tail in FILENAMES_TO_TAIL:
        base_name = filename_to_tail.split("/")[-1].replace(".", "-")  # XXX
        logger.debug(f'Run container for: {base_name}')
        client.containers.run(
            "debian:buster-slim",
            command=f"tail --retry --follow=name {filename_to_tail}",
            detach=True,
            labels={
                'name': "nginx-log-tailer",
                'version': timestamp,
                'ngx_log_file': base_name
            },
            volumes={
                logs_volume.name:
                    {
                        'bind': '/var/log/nginx/',
                        'mode': 'ro'
                    },
            },
            name=f"nginx-{base_name}-tailer-{timestamp}",
            restart_policy=DEFAULT_RESTART_POLICY
        )
    return client.containers.run(
        image_id,
        detach=True,
        # XXX revisit this with the nat switcher and 0-downtime deploy stuff
        # ports={
        #     '80/tcp': ('0.0.0.0', None),  # None means docker chooses an available port
        #     '443/tcp': ('0.0.0.0', None), # XXX making these private ports public for now
        # },
        ports={
            '80/tcp': ('0.0.0.0', 80),
            '443/tcp': ('0.0.0.0', 443),
        },
        labels={
            'name': "nginx",
            'version': timestamp
        },
        # XXX making a volume for access logs, and a bind mount for the banjax-next stuff...
        # think about this.
        volumes={
            logs_volume.name:
                {
                    'bind': '/var/log/nginx/',
                    'mode': 'rw'
                },
            '/root/banjax-next/':
                {
                    'bind': '/var/log/banjax-next/',
                    'mode': 'rw'
                }
        },
        name=f"nginx-{timestamp}",
        restart_policy=DEFAULT_RESTART_POLICY
    )


def start_new_bind_container(client, image_id, timestamp):
    return client.containers.run(
        image_id,
        detach=True,
        ports={
            '53/udp': ('0.0.0.0', '53'),
            '53/tcp': ('0.0.0.0', '53'),
            '8085/tcp': ('0.0.0.0', '8085'),  # XXX for doh-proxy
        },
        labels={
            'name': "bind-server",
            'version': timestamp
        },
        name="bind-server",
        restart_policy=DEFAULT_RESTART_POLICY
    )


def start_new_certbot_container(client, image_id, timestamp):
    return client.containers.run(
        image_id,
        detach=True,
        labels={
            'name': "certbot",
            'version': timestamp
        },
        name="certbot",
        restart_policy=DEFAULT_RESTART_POLICY,
        # XXX should we specify container id instead?
        network_mode="container:bind-server"
    )


def start_new_banjax_next_container(client, image_id, timestamp):
    # XXX consider a different approach (making the caller pass in the network and fs namespaces?)
    nginx_containers = client.containers.list(
        filters={"label": f"name=nginx"}
    )

    if len(nginx_containers) != 1:
        logger.error(
            f"start_new_banjax_next_container() expected to find a single "
            f"running nginx container (whose namespaces we can join)"
        )
        raise Exception

    nginx_container = nginx_containers[0]

    # XXX bad duplication with the nginx log tailers above
    filenames_to_tail = [
        "/var/log/banjax-next/gin.log",
        "/var/log/banjax-next/metrics.log",
    ]
    for filename in filenames_to_tail:
        base_name = filename.split("/")[-1].replace(".", "-")  # XXX
        client.containers.run(
            "debian:buster-slim",
            command=f"tail --retry --follow=name {filename}",
            detach=True,
            labels={
                'name': "banjax-next-log-tailer",
                'version': timestamp,
                'banjax_next_log_file': base_name
            },
            volumes={  # XXX check out volumes_from?
                '/root/banjax-next/':  # XXX
                    {
                        'bind': '/var/log/banjax-next/',
                        'mode': 'ro'
                    }
            },
            name=f"banjax-next-log-{base_name}",
            restart_policy={"Name": "on-failure", "MaximumRetryCount": 5}
        )

    return client.containers.run(
        image_id,
        detach=True,
        labels={
            'name': "banjax-next",
            'version': timestamp
        },
        volumes={  # XXX check out volumes_from?
            '/root/banjax-next/':  # XXX
                {
                    'bind': '/var/log/banjax-next/',
                    'mode': 'rw'
                }
        },
        name="banjax-next",
        restart_policy=DEFAULT_RESTART_POLICY,
        cap_add=["NET_ADMIN"],
        # XXX should we specify container id instead?
        network_mode=f"container:{nginx_container.name}"
    )


def start_new_origin_container(client, image_id, timestamp):
    return client.containers.run(
        image_id,
        detach=True,
        ports={
            '8080/tcp': ('0.0.0.0', '8080'),
        },
        labels={
            'name': "origin-server",
            'version': timestamp
        },
        name="origin-server",
        restart_policy=DEFAULT_RESTART_POLICY
    )


def start_new_pebble_container(client, image_id, timestamp):
    return client.containers.run(
        image_id,
        command="pebble -config /test/config/pebble-config.json -dnsserver 127.0.0.1:53",
        # command="sleep infinity",
        detach=True,
        labels={
            'name': "pebble",
            'version': timestamp
        },
        environment={
            'PEBBLE_VA_NOSLEEP': "1",
        },
        name="pebble",
        restart_policy=DEFAULT_RESTART_POLICY,
        # XXX should we specify container id instead?
        network_mode="container:bind-server"
    )


def start_new_doh_proxy_container(client, image_id, timestamp):
    return client.containers.run(
        image_id,
        detach=True,
        labels={
            'name': "doh-proxy",
            'version': timestamp
        },
        name="doh-proxy",
        restart_policy=DEFAULT_RESTART_POLICY,
        # XXX should we specify container id instead?
        network_mode="container:bind-server"
    )


def start_new_elasticsearch_container(client, image_id, timestamp):
    return client.containers.run(
        image_id,
        detach=True,
        ports={
            '9200/tcp': ('0.0.0.0', '9200'),
        },
        labels={
            'name': "elasticsearch",
            'version': timestamp
        },
        environment={
            "discovery.type": "single-node",
            "bootstrap.memory_lock": "true",
            "ES_JAVA_OPTS": "-Xms512m -Xmx512m",
            "xpack.security.enabled": "true",
            "xpack.security.transport.ssl.enabled": "true",
            "xpack.security.transport.ssl.key": "/usr/share/elasticsearch/config/instance.key",
            "xpack.security.transport.ssl.certificate": "/usr/share/elasticsearch/config/instance.crt",
            "xpack.security.http.ssl.enabled": "true",
            "xpack.security.http.ssl.key": "/usr/share/elasticsearch/config/instance.key",
            "xpack.security.http.ssl.certificate": "/usr/share/elasticsearch/config/instance.crt",
        },
        ulimits=[
            docker.types.Ulimit(name='memlock', soft=-1, hard=-1),
        ],
        name="elasticsearch",
        restart_policy=DEFAULT_RESTART_POLICY,
    )

# XXX this is weird and should be cleaned up
def get_persisted_config():
    p_conf = {}
    with open(get_persisted_config_yml_path(), "r") as f:
        p_conf = yaml.load(f)
    return p_conf


def start_new_kibana_container(client, image_id, timestamp, config):
    return client.containers.run(
        image_id,
        detach=True,
        ports={
            '5601/tcp': ('0.0.0.0', '5601'),
        },
        labels={
            'name': "kibana",
            'version': timestamp
        },
        environment={
            # "ELASTICSEARCH_URL": f"http://{config['controller']['ip']}:9200",
            "ELASTICSEARCH_HOSTS": f"https://{config['controller']['ip']}:9200",
            # "SERVER_SSL_ENABLED": "true",
            "ELASTICSEARCH_SSL_CERTIFICATEAUTHORITIES": "/etc/kibana/ca.crt",
            "ELASTICSEARCH_SSL_VERIFICATIONMODE": "none",
            "ELASTICSEARCH_USERNAME": "elastic",
            "ELASTICSEARCH_PASSWORD": get_persisted_config()['elastic_password'],
        },
        volumes={
            '/var/run/':  # XXX
                {
                    'bind': '/var/run/',
                    'mode': 'ro'
                }
        },
        name="kibana",
        restart_policy=DEFAULT_RESTART_POLICY,
    )

def get_hostname_and_dnet(config, client):
    hostname = f"{client.info().get('Name')}"
    for c_edge in config['edges']:
        if c_edge['hostname'].startswith(hostname):
            return (hostname, c_edge['dnet'])
    return hostname, "controller"


def start_new_metricbeat_container(client, image_id, timestamp, config):
    (edge_name, dnet) = get_hostname_and_dnet(config, client)

    # XXX consider a different approach (making the caller pass in the network and fs namespaces?)
    nginx_containers = client.containers.list(
        filters={"label": f"name=nginx"}
    )

    if len(nginx_containers) != 1:
        print(f"start_new_metricbeat_container() expected to find a single running nginx container (whose namespaces we can join)")
        raise Exception

    nginx_container = nginx_containers[0]

    return client.containers.run(
        image_id,
        detach=True,
        user="root",
        labels={
            'name': "metricbeat",
            'version': timestamp
        },
        environment={
            # "ELASTICSEARCH_URL": f"http://{config['controller']['ip']}:9200",   # XXX authentication, encryption
            "ELASTICSEARCH_HOST": f"https://{config['controller']['ip']}:9200",
            "KIBANA_HOST": f"https://{config['controller']['ip']}:5601",
            "ELASTICSEARCH_PASSWORD": get_persisted_config()['elastic_password'],
            "DEFLECT_EDGE_NAME": edge_name,
            "DEFLECT_DNET": dnet,
        },
        volumes={
            '/var/run/':  # XXX
                {
                    'bind': '/var/run/',
                    'mode': 'ro'
                },
            '/sys/fs/cgroup':  # XXX
                {
                    'bind': '/hostfs/sys/fs/cgroup',
                    'mode': 'ro'
                },
            '/proc':  # XXX
                {
                    'bind': '/hostfs/proc',
                    'mode': 'ro'
                },
        },
        name="metricbeat",
        network_mode=f"container:{nginx_container.name}",
        # extra_hosts={"nginx": "127.0.0.1"}, # XXX can't set Host header in metricbeat
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
    )


def start_new_filebeat_container(client, image_id, timestamp, config):
    (edge_name, dnet) = get_hostname_and_dnet(config, client)

    return client.containers.run(
        image_id,
        detach=True,
        user="root",  # XXX needed?
        labels={
            'name': "filebeat",
            'version': timestamp
        },
        hostname=edge_name,
        environment={
            # "ELASTICSEARCH_URL": f"http://{config['controller']['ip']}:9200",   # XXX authentication, encryption
            "ELASTICSEARCH_HOST": f"https://{config['controller']['ip']}:9200",
            "KIBANA_HOST": f"https://{config['controller']['ip']}:5601",
            "ELASTICSEARCH_PASSWORD": get_persisted_config()['elastic_password'],
            "DEFLECT_EDGE_NAME": edge_name,
            "DEFLECT_DNET": dnet,
        },
        volumes={
            '/var/run/':  # XXX
                {
                    'bind': '/var/run/',
                    'mode': 'ro'
                },
            '/var/lib/docker/containers/':  # XXX
                {
                    'bind': '/var/lib/docker/containers/',
                    'mode': 'ro'
                },
        },
        name="filebeat",
        restart_policy=DEFAULT_RESTART_POLICY,
    )


def start_new_legacy_filebeat_container(client, image_id, timestamp, config):
    (edge_name, dnet) = get_hostname_and_dnet(config, client)

    return client.containers.run(
        image_id,
        detach=True,
        user="root",  # XXX needed?
        labels={
            'name': "legacy-filebeat",
            'version': timestamp
        },
        hostname=edge_name,
        environment={
            # "LOGSTASH_HOST": f"{config['controller']['ip']}:5044",   # XXX authentication, encryption
            "LOGSTASH_HOST": "opsdash.deflect.ca:5044",
            "DEFLECT_EDGE_NAME": edge_name,
            "DEFLECT_DNET": dnet,
        },
        volumes={
            '/root/banjax-next/':
                {
                    'bind': '/var/log/banjax-next/',
                    'mode': 'ro'
                }
        },
        name="legacy-filebeat",
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
    )


def start_new_legacy_logstash_container(client, image_id, timestamp, config):
    return client.containers.run(
        image_id,
        detach=True,
        ports={
            '5044/tcp': ('0.0.0.0', '5044'),
        },
        labels={
            'name': "legacy-logstash",
            'version': timestamp
        },
        environment={
            "ELASTICSEARCH_HOST": f"{config['controller']['ip']}:9200",
        },
        name="legacy-logstash",
        restart_policy={"Name": "on-failure", "MaximumRetryCount": 5},
    )


def start_new_container(client, image_name, new_image, image_build_timestamp, config):
    # XXX clean up
    # todo: a factory
    if image_name == "nginx":
        new_container = start_new_nginx_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "bind-server":
        new_container = start_new_bind_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "certbot":
        new_container = start_new_certbot_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "pebble":
        new_container = start_new_pebble_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "banjax-next":
        new_container = start_new_banjax_next_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "origin-server":
        new_container = start_new_origin_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "doh-proxy":
        new_container = start_new_doh_proxy_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "filebeat":
        new_container = start_new_filebeat_container(
            client, new_image.id, image_build_timestamp, config)
    elif image_name == "legacy-filebeat":
        new_container = start_new_legacy_filebeat_container(
            client, new_image.id, image_build_timestamp, config)
    elif image_name == "elasticsearch":
        new_container = start_new_elasticsearch_container(
            client, new_image.id, image_build_timestamp)
    elif image_name == "kibana":
        new_container = start_new_kibana_container(
            client, new_image.id, image_build_timestamp, config)
    elif image_name == "metricbeat":
        new_container = start_new_metricbeat_container(
            client, new_image.id, image_build_timestamp, config)
    else:
        msg = f"!!! bug !!! need start_new_{image_name} function call"
        logger.error(msg)
        raise Exception(msg)
    return new_container


def kill_containers_with_label(client, label):
    logger.info(f"killing containers with label or name {label}")
    print(f"killing containers with label or name {label}")
    # XXX this doesn't work TODO: fixme
    containers1 = client.containers.list(
        all=True,
        filters={'label': f"name={label}"}
    )
    containers2 = client.containers.list(
        all=True,
        filters={'name': label}
    )
    for container in containers1 + containers2:
        logger.info(f"killing {container} with label or name {label}")
        print(f"killing {container} with label or name {label}")
        # XXX ugh all of this
        try:
            container.kill()
        except:
            logger.error('Could not kill container')
            pass
        try:
            container.remove()  # XXX just so i can use a name later... re-think
        except:
            logger.error('Could not remove container')
            pass


def get_all_sites():
    logger.info('Getting all sites')
    old_client_sites = {}
    old_client_sites_timestamp = None
    # with open("input/current/old-sites.yml", "r") as f:
    while True:
        try:
            with open(get_sites_yml_path(), "r") as f:
                old_client_sites_dict = yaml.load(f.read(), Loader=yaml.SafeLoader)
                old_client_sites_timestamp = old_client_sites_dict["timestamp"]
                old_client_sites = old_client_sites_dict["remap"]
                break
        except FileNotFoundError:
            logger.info(f"didn't find anything at {get_sites_yml_path()}, sleeping...")
            time.sleep(5)

    time_from_inside_file = datetime.fromtimestamp(float(old_client_sites_timestamp)/1000.0)
    formatted_time = time_from_inside_file.strftime("%Y-%m-%d_%H:%M:%S")

    logger.debug('Getting new client sites: old_to_new_site_dict.main')
    new_client_sites = old_to_new_site_dict.main(
        old_client_sites, old_client_sites_timestamp
    )

    logger.debug('Getting new system_sites')
    system_sites = {}
    with open("input/system-sites.yml", "r") as f:
        system_sites = yaml.load(f.read(), Loader=yaml.SafeLoader)

    all_sites = {'client': new_client_sites, 'system': system_sites}
    logger.debug(f'All sites:{json.dumps(all_sites, indent=2)}')
    return all_sites, formatted_time
