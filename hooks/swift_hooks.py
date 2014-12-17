#!/usr/bin/python

import os
import sys
import subprocess

from swift_utils import (
    SwiftProxyCharmException,
    register_configs,
    restart_map,
    determine_packages,
    ensure_swift_dir,
    SWIFT_RINGS,
    get_www_dir,
    initialize_ring,
    SWIFT_HA_RES,
    get_zone,
    do_openstack_upgrade,
    setup_ipv6,
    update_rings,
    balance_rings,
    fully_synced,
    sync_proxy_rings,
    broadcast_rings_available,
    mark_www_rings_deleted,
    SwiftProxyClusterRPC,
    get_first_available_value,
    all_responses_equal,
    ensure_www_dir_permissions,
)

import charmhelpers.contrib.openstack.utils as openstack
from charmhelpers.contrib.hahelpers.cluster import (
    is_elected_leader,
    is_crm_leader
)
from charmhelpers.core.hookenv import (
    config,
    local_unit,
    remote_unit,
    unit_get,
    relation_set,
    relation_ids,
    relation_get,
    related_units,
    log,
    DEBUG,
    INFO,
    WARNING,
    ERROR,
    Hooks, UnregisteredHookError,
    open_port,
)
from charmhelpers.core.host import (
    service_restart,
    service_stop,
    service_start,
    restart_on_change,
)
from charmhelpers.fetch import (
    apt_install,
    apt_update,
)
from charmhelpers.payload.execd import execd_preinstall
from charmhelpers.contrib.openstack.ip import (
    canonical_url,
    PUBLIC,
    INTERNAL,
    ADMIN,
)
from charmhelpers.contrib.network.ip import (
    get_iface_for_address,
    get_netmask_for_address,
    get_address_in_network,
    get_ipv6_addr,
    is_ipv6,
    format_ipv6_addr,
)
from charmhelpers.contrib.openstack.context import ADDRESS_TYPES

extra_pkgs = [
    "haproxy",
    "python-jinja2"
]

hooks = Hooks()
CONFIGS = register_configs()


@hooks.hook('install')
def install():
    execd_preinstall()
    src = config('openstack-origin')
    if src != 'distro':
        openstack.configure_installation_source(src)

    apt_update(fatal=True)
    rel = openstack.get_os_codename_install_source(src)
    pkgs = determine_packages(rel)
    apt_install(pkgs, fatal=True)
    apt_install(extra_pkgs, fatal=True)
    ensure_swift_dir()

    if is_elected_leader(SWIFT_HA_RES):
        log("Leader established, generating ring builders", level=INFO)
        # initialize new storage rings.
        for path in SWIFT_RINGS.itervalues():
            initialize_ring(path,
                            config('partition-power'),
                            config('replicas'),
                            config('min-hours'))

    # configure a directory on webserver for distributing rings.
    ensure_www_dir_permissions(get_www_dir())


@hooks.hook('config-changed')
@restart_on_change(restart_map())
def config_changed():
    if config('prefer-ipv6'):
        setup_ipv6()

    configure_https()
    open_port(config('bind-port'))

    # Determine whether or not we should do an upgrade.
    if openstack.openstack_upgrade_available('python-swift'):
        do_openstack_upgrade(CONFIGS)

    update_rings(min_part_hours=config('min-hours'))

    if not config('disable-ring-balance') and is_elected_leader(SWIFT_HA_RES):
        # Try ring balance. If rings are balanced, no sync will occur.
        balance_rings()

    for r_id in relation_ids('identity-service'):
        keystone_joined(relid=r_id)


@hooks.hook('identity-service-relation-joined')
def keystone_joined(relid=None):
    if not is_elected_leader(SWIFT_HA_RES):
        return

    port = config('bind-port')
    admin_url = '%s:%s' % (canonical_url(CONFIGS, ADMIN), port)
    internal_url = '%s:%s/v1/AUTH_$(tenant_id)s' % \
        (canonical_url(CONFIGS, INTERNAL), port)
    public_url = '%s:%s/v1/AUTH_$(tenant_id)s' % \
        (canonical_url(CONFIGS, PUBLIC), port)
    relation_set(service='swift',
                 region=config('region'),
                 public_url=public_url,
                 internal_url=internal_url,
                 admin_url=admin_url,
                 requested_roles=config('operator-roles'),
                 relation_id=relid)


@hooks.hook('identity-service-relation-changed')
@restart_on_change(restart_map())
def keystone_changed():
    configure_https()


@hooks.hook('swift-storage-relation-joined')
def storage_joined():
    if not is_elected_leader(SWIFT_HA_RES):
        log("New storage relation joined - stopping proxy until ring builder "
            "synced", level=INFO)
        service_stop('swift-proxy')

        # This unit is not currently responsible for distributing rings but
        # may become so at some time in the future so we do this to avoid the
        # possibility of storage nodes getting out-of-date rings by deprecating
        # any existing ones from the www dir.
        mark_www_rings_deleted()


@hooks.hook('swift-storage-relation-changed')
@restart_on_change(restart_map())
def storage_changed():
    """Storage relation.

    Only the leader unit can update and distribute rings so if we are not the
    leader we ignore this event and wait for a resync request from the leader.
    """
    if not is_elected_leader(SWIFT_HA_RES):
        log("Not the leader - ignoring storage relation until leader ready.",
            level=DEBUG)
        return

    log("Leader established, updating ring builders", level=INFO)
    addr = relation_get('private-address')
    if config('prefer-ipv6'):
        host_ip = format_ipv6_addr(addr)
        if not host_ip:
            errmsg = ("Did not get IPv6 address from storage relation "
                      "(got=%s)" % (addr))
            raise SwiftProxyCharmException(errmsg)
    else:
        host_ip = openstack.get_host_ip(addr)

    zone = get_zone(config('zone-assignment'))
    node_settings = {
        'ip': host_ip,
        'zone': zone,
        'account_port': relation_get('account_port'),
        'object_port': relation_get('object_port'),
        'container_port': relation_get('container_port'),
    }

    if None in node_settings.itervalues():
        missing = [k for k, v in node_settings.iteritems() if v is None]
        log("Relation not ready - some required values not provided by "
            "relation (missing=%s)" % (', '.join(missing)), level=INFO)
        return None

    for k in ['zone', 'account_port', 'object_port', 'container_port']:
        node_settings[k] = int(node_settings[k])

    CONFIGS.write_all()

    # Allow for multiple devs per unit, passed along as a : separated list
    # Update and balance rings.
    devs = relation_get('device')
    if devs:
        node_settings['devices'] = devs.split(':')

    update_rings(node_settings)
    # Restart proxy here in case no config changes made (so
    # restart_on_change() ineffective).
    service_restart('swift-proxy')


@hooks.hook('swift-storage-relation-broken')
@restart_on_change(restart_map())
def storage_broken():
    CONFIGS.write_all()


@hooks.hook('cluster-relation-joined')
def cluster_joined(relation_id=None):
    for addr_type in ADDRESS_TYPES:
        netaddr_cfg = 'os-{}-network'.format(addr_type)
        address = get_address_in_network(config(netaddr_cfg))
        if address:
            settings = {'{}-address'.format(addr_type): address}
            relation_set(relation_id=relation_id, relation_settings=settings)

    if config('prefer-ipv6'):
        private_addr = get_ipv6_addr(exc_list=[config('vip')])[0]
        relation_set(relation_id=relation_id,
                     relation_settings={'private-address': private_addr})
    else:
        private_addr = unit_get('private-address')


def all_peers_stopped(responses):
    """Establish whether all peers have stopped their proxy services.

    Each peer unit will set stop-proxy-service-ack to rq value to indicate that
    it has stopped its proxy service. We wait for all units to be stopped
    before triggering a sync. Peer services will be  restarted once their rings
    are synced with the leader.

    To be safe, default expectation is that api is still running.
    """
    rq_key = SwiftProxyClusterRPC.KEY_STOP_PROXY_SVC
    ack_key = SwiftProxyClusterRPC.KEY_STOP_PROXY_SVC_ACK
    token = relation_get(attribute=rq_key, unit=local_unit())
    if not token or token != responses[0].get(ack_key):
        log("Unmatched token in ack (expected=%s, got=%s)" %
            (token, responses[0].get(ack_key)), level=DEBUG)
        return False

    if not all_responses_equal(responses, ack_key):
        return False

    return True


def cluster_leader_actions():
    """Cluster relation hook actions to be performed by leader units.

    NOTE: must be called by leader from cluster relation hook.
    """
    log("Cluster changed by unit=%s (local is leader)" % (remote_unit()),
        level=DEBUG)

    # If we have received an ack, check other units
    settings = relation_get() or {}
    ack_key = SwiftProxyClusterRPC.KEY_STOP_PROXY_SVC_ACK

    # Protect against leader changing mid-sync
    if settings.get(SwiftProxyClusterRPC.KEY_STOP_PROXY_SVC):
        log("Sync request received yet this is leader unit. This would "
            "indicate that the leader has changed mid-sync - stopping proxy "
            "and notifying peers", level=ERROR)
        service_stop('swift-proxy')
        SwiftProxyClusterRPC().notify_leader_changed()
        return
    elif ack_key in settings:
        # Find out if all peer units have been stopped.
        responses = []
        for rid in relation_ids('cluster'):
            for unit in related_units(rid):
                responses.append(relation_get(rid=rid, unit=unit))

        # Ensure all peers stopped before starting sync
        if all_peers_stopped(responses):
            key = 'peers-only'
            if not all_responses_equal(responses, key, must_exist=False):
                msg = ("Did not get equal response from every peer unit for "
                       "'%s'" % (key))
                raise SwiftProxyCharmException(msg)

            peers_only = int(get_first_available_value(responses, key,
                                                       default=0))
            log("Syncing rings and builders (peers-only=%s)" % (peers_only),
                level=DEBUG)
            broadcast_rings_available(storage=not peers_only)
        else:
            log("Not all peer apis stopped - skipping sync until all peers "
                "ready (got %s)" % (responses), level=INFO)
    else:
        # Otherwise it might be a new swift-proxy unit so tell it to sync
        # rings. Note that broker info may already be present in the cluster
        # relation so don't use a trigger otherwise the hook will re-fire on
        # all peers.
        broadcast_rings_available(storage=False, use_trigger=False)

    CONFIGS.write_all()


def cluster_non_leader_actions():
    """Cluster relation hook actions to be performed by non-leader units.

    NOTE: must be called by non-leader from cluster relation hook.
    """
    log("Cluster changed by unit=%s (local is non-leader)" % (remote_unit()),
        level=DEBUG)
    settings = relation_get() or {}

    # Check whether we have been requested to stop proxy service
    rq_key = SwiftProxyClusterRPC.KEY_STOP_PROXY_SVC
    token = settings.get(rq_key, None)
    if token:
        log("Peer request to stop proxy service received (%s) - sending ack" %
            (token), level=INFO)
        service_stop('swift-proxy')
        peers_only = settings.get('peers-only', None)
        rq = SwiftProxyClusterRPC().stop_proxy_ack(echo_token=token,
                                                   echo_peers_only=peers_only)
        relation_set(relation_settings=rq)
        return

    # Check if there are any builder files we can sync from the leader.
    log("Non-leader peer - checking if updated rings available", level=DEBUG)
    broker = settings.get('builder-broker', None)
    if not broker:
        log("No update available", level=DEBUG)
        service_start('swift-proxy')
        return

    builders_only = int(settings.get('sync-only-builders', 0))
    path = os.path.basename(get_www_dir())
    try:
        sync_proxy_rings('http://%s/%s' % (broker, path),
                         rings=not builders_only)
    except subprocess.CalledProcessError:
        log("Ring builder sync failed, builders not yet available - "
            "leader not ready?", level=WARNING)
        return None

    # Re-enable the proxy once all builders and rings are synced
    if fully_synced():
        log("Ring builders synced - starting proxy", level=INFO)
        CONFIGS.write_all()
        service_start('swift-proxy')
    else:
        log("Not all builders and rings synced yet - waiting for peer sync "
            "before starting proxy", level=INFO)


@hooks.hook('cluster-relation-changed',
            'cluster-relation-departed')
@restart_on_change(restart_map())
def cluster_changed():
    key = SwiftProxyClusterRPC.KEY_NOTIFY_LEADER_CHANGED
    leader_changed = relation_get(attribute=key)
    if leader_changed:
        log("Leader changed notification received from peer unit. Since this "
            "most likely occurred during a ring sync proxies will be "
            "disabled until the leader is restored and a fresh sync request "
            "is set out", level=WARNING)
        service_stop("swift-proxy")
        return

    if is_elected_leader(SWIFT_HA_RES):
        cluster_leader_actions()
    else:
        cluster_non_leader_actions()


@hooks.hook('ha-relation-changed')
def ha_relation_changed():
    clustered = relation_get('clustered')
    if clustered and is_crm_leader(SWIFT_HA_RES):
        log("Cluster configured, notifying other services and updating "
            "keystone endpoint configuration", level=INFO)
        # Tell all related services to start using
        # the VIP instead
        for r_id in relation_ids('identity-service'):
            keystone_joined(relid=r_id)


@hooks.hook('ha-relation-joined')
def ha_relation_joined():
    # Obtain the config values necessary for the cluster config. These
    # include multicast port and interface to bind to.
    corosync_bindiface = config('ha-bindiface')
    corosync_mcastport = config('ha-mcastport')
    vip = config('vip')
    if not vip:
        msg = 'Unable to configure hacluster as vip not provided'
        raise SwiftProxyCharmException(msg)

    # Obtain resources
    resources = {'res_swift_haproxy': 'lsb:haproxy'}
    resource_params = {'res_swift_haproxy': 'op monitor interval="5s"'}

    vip_group = []
    for vip in vip.split():
        if is_ipv6(vip):
            res_swift_vip = 'ocf:heartbeat:IPv6addr'
            vip_params = 'ipv6addr'
        else:
            res_swift_vip = 'ocf:heartbeat:IPaddr2'
            vip_params = 'ip'

        iface = get_iface_for_address(vip)
        if iface is not None:
            vip_key = 'res_swift_{}_vip'.format(iface)
            resources[vip_key] = res_swift_vip
            resource_params[vip_key] = (
                'params {ip}="{vip}" cidr_netmask="{netmask}"'
                ' nic="{iface}"'.format(ip=vip_params,
                                        vip=vip,
                                        iface=iface,
                                        netmask=get_netmask_for_address(vip))
            )
            vip_group.append(vip_key)

    if len(vip_group) >= 1:
        relation_set(groups={'grp_swift_vips': ' '.join(vip_group)})

    init_services = {'res_swift_haproxy': 'haproxy'}
    clones = {'cl_swift_haproxy': 'res_swift_haproxy'}

    relation_set(init_services=init_services,
                 corosync_bindiface=corosync_bindiface,
                 corosync_mcastport=corosync_mcastport,
                 resources=resources,
                 resource_params=resource_params,
                 clones=clones)


def configure_https():
    """Enables SSL API Apache config if appropriate and kicks identity-service
    with any required api updates.
    """
    # need to write all to ensure changes to the entire request pipeline
    # propagate (c-api, haprxy, apache)
    CONFIGS.write_all()
    if 'https' in CONFIGS.complete_contexts():
        cmd = ['a2ensite', 'openstack_https_frontend']
        subprocess.check_call(cmd)
    else:
        cmd = ['a2dissite', 'openstack_https_frontend']
        subprocess.check_call(cmd)

    # Apache 2.4 required enablement of configuration
    if os.path.exists('/usr/sbin/a2enconf'):
        subprocess.check_call(['a2enconf', 'swift-rings'])

    for rid in relation_ids('identity-service'):
        keystone_joined(relid=rid)

    env_vars = {'OPENSTACK_SERVICE_SWIFT': 'proxy-server',
                'OPENSTACK_PORT_API': config('bind-port'),
                'OPENSTACK_PORT_MEMCACHED': 11211}
    openstack.save_script_rc(**env_vars)


def main():
    try:
        hooks.execute(sys.argv)
    except UnregisteredHookError as e:
        log('Unknown hook {} - skipping.'.format(e), level=DEBUG)


if __name__ == '__main__':
    main()
