#!/usr/bin/env python3
import os
import json
import shutil
from subprocess import run, CalledProcessError, PIPE
from collections import defaultdict
from charms.reactive import (
    when,
    when_not,
    set_flag,
    when_not_all,
    hook,
    clear_flag,
    when_any,
)
from charms.reactive.relations import endpoint_from_flag
from charmhelpers.core.hookenv import log, is_leader, status_set
from charmhelpers.core import unitdata, hookenv
from charms.layer.resourcefactory import ResourceFactory
from charms.layer.k8shelpers import (
    delete_resources_by_label,
    get_label_values_per_deployer,
    add_label_to_resource,
    get_worker_node_ips,
    resource_owner,
)


# Add kubectl to PATH
os.environ['PATH'] += os.pathsep + os.path.join(os.sep, 'snap', 'bin')
config = hookenv.config()
deployer = os.environ['JUJU_UNIT_NAME'].split('/')[0]


@when_not('kube-host.available')
def wait_for_k8s():
    status_set('blocked', 'Waiting for relation to Kubernetes-master')


@when('kube-host.available')
@when_not('kube-deployer.connected')
def set_active(kube):
    status_set('active', 'Ready')


@when_not('deployer.installed')
def install_deployer():
    # General deployer options
    deployers_path = '/home/kubedeployer/.config/kubedeployers'
    deployer_path = deployers_path + '/' + os.environ['JUJU_UNIT_NAME'].replace('/', '-')
    # Save then in the kv store
    namespace_selector = 'ns'
    unitdata.kv().set('deployers_path', deployers_path)
    unitdata.kv().set('deployer_path', deployer_path)
    unitdata.kv().set('juju_app_selector', 'juju-app')
    unitdata.kv().set('deployer_selector', 'deployer')
    unitdata.kv().set('namespace_selector', namespace_selector)
    # Setup dir structure
    log('Setting up deployer dirs in: ' + deployer_path)
    global_dirs = ['namespaces', 'network-policies']
    for gd in global_dirs:
        if not os.path.exists(deployers_path + '/' + gd):
            os.makedirs(deployers_path + '/' + gd)
    dirs = ['resources']
    for d in dirs:
        if not os.path.exists(deployer_path + '/' + d):
            os.makedirs(deployer_path + '/' + d)
    # Setup the default namespace
    add_label_to_resource('default', namespace_selector + '=default', 'namespace', 'default', True)
    set_flag('deployer.installed')


@when('endpoint.kubernetes-deployer.available', 'kube-host.available')
def new_resource_request(dep, kube):
    if not is_leader():
        return
    configure_namespace()
    requests = dep.get_resource_requests()
    clean_deployer_config(['resources'])
    application_names = {}
    for request in requests:
        if not request['uuid']:
            continue
        application_names[request['uuid'].split('/')[0]] = request['resource']
    used_apps = unitdata.kv().get('used_apps', [])
    unitdata.kv().set('used_apps', list(set(used_apps) | application_names.keys()))
    error_states = {}
    for app, resources in application_names.items():
        if not resources:
            continue
        unique_id = 0
        for resource in resources:
            if resource_name_duplicate(resource, app):
                error_states[app] = {'error': 'Duplicate name for resource: '
                                               + resource['metadata']['name']}
                continue
            prepared_request = {
                'name': app,
                'resource': resource,
                'namespace': config.get('namespace').rstrip(),
                'unique_id': unique_id,
            }
            unique_id += 1
            pre_resource = ResourceFactory.create_resource('preparedresource', prepared_request)
            pre_resource.write_resource_file()
            pre_resource.create_resource()
    status = check_predefined_resources()
    status.update(error_states)
    dep.send_status(status)
    dep.send_worker_ips(get_worker_node_ips())
    set_flag('resources.created')


"""
CLEANUP STATES
"""
@when('kube-host.available')
@when_not('endpoint.kubernetes-deployer.available')
def call_cleanup(kube):
    cleanup()


@when_any('resources.created')
def cleanup():
    # Iterate over all resources with label from this deployer
    # Remove all which are not needed anymore
    if not is_leader():
        return
    needed_apps = unitdata.kv().get('used_apps', [])
    all_apps = get_label_values_per_deployer(config.get('namespace').rstrip(),
                                             unitdata.kv().get('juju_app_selector'),
                                             unitdata.kv().get('deployer_selector') + '=' +
                                             os.environ['JUJU_UNIT_NAME'].split('/')[0])
    for app in all_apps:
        if app not in needed_apps:
            # Remove resource via label
            delete_resources_by_label(config.get('namespace').rstrip(),
                                      ['all,cm,secrets'],
                                      unitdata.kv().get('juju_app_selector') + '=' + app)
    unitdata.kv().set('used_apps', [])

    if config.changed('namespace') and config.previous('namespace').rstrip():
        log('Checking if previous namespace still has resources, if not delete namespace (' +
            config.previous('namespace').rstrip() + ')')
        namespace = ResourceFactory.create_resource('namespace', {'name': config.previous('namespace').rstrip()})
        namespace.delete_resource()
    clear_flag('resources.created')
    clear_flag('endpoint.kubernetes-deployer.cleanup')


@hook('stop')
def clean_deployer_configs():
    path = unitdata.kv().get('deployer_path') + '/resources'
    for file in os.listdir(path):
        try:
            run(['kubectl', 'delete', '-f', path + '/' + file])
        except CalledProcessError as e:
            log(e)
    shutil.rmtree(unitdata.kv().get('deployer_path'))


@when('deployer.installed')
def create_policies():
    if not is_leader():
        return
    configure_namespace()
    request = {
        'namespace': config['namespace'].rstrip(''),
        'name': os.environ['JUJU_UNIT_NAME'].replace('/', '-')
    }
    policy = ResourceFactory.create_resource('network-policy', request)
    if not config['isolated']:
        policy.delete_resource()
        return
    if config.changed('namespace') and config.previous('namespace').rstrip():
        policy.delete_resource()
    policy.write_resource_file()
    policy.create_resource()


def clean_deployer_config(resources):
    """Remove all resource files from this deployer.
    
    Args:
        resources (list): name of resource folder
    """
    if resources is None:
        return
    for resource in resources:
        path = unitdata.kv().get('deployer_path') + '/' + resource
        shutil.rmtree(path)
        os.mkdir(path)


def configure_namespace():
    namespace = ResourceFactory.create_resource('namespace', {'name': config.get('namespace', 'default').rstrip(),
                                                              'deployer': deployer})
    namespace.write_resource_file()
    namespace.create_resource()
    # Check if config.namespace changed
    if config.changed('namespace') and config.previous('namespace'):
        # Remove all resources from previous namespace created by this deployer
        prev_namespace = ResourceFactory.create_resource('namespace',
                                                         {'name': config.previous('namespace').rstrip(),
                                                          'deployer': deployer})
        prev_namespace.delete_namespace_resources()


def check_predefined_resources():
    """Return `kubectl get` about resources in deployer_path/resources.
    
    Returns:
        {
            'juju_unit_name': {...},
            ...
        }
    """
    result = {}
    path = unitdata.kv().get('deployer_path') + '/resources'
    for file in os.listdir(path):
        juju_unit_name = file.rsplit('-', 1)[0]
        if juju_unit_name not in result:
            result[juju_unit_name] = []
        try:
            cmd = run(['kubectl', 'get', '-f', path + '/' + file, '-o', 'json'], stdout=PIPE)
            cmd.check_returncode()
            result[juju_unit_name].append(json.loads(cmd.stdout.decode('utf-8')))
        except CalledProcessError:
            result[juju_unit_name] = False
    return result


def resource_name_duplicate(resource, app):
    """Check if the resource name already exists 
    in this namespace

    Args:
        resource (dict)
        app (str): name of the juju unit requesting the resource
    Returns:
        True | False
    """
    owner = resource_owner(config.get('namespace', 'default'),
                           resource['metadata']['name'], 
                           unitdata.kv().get('juju_app_selector')) 
    if owner and owner != app:
        return True
    return False
