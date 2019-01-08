#!/usr/bin/env python3
import os
import json
import shutil
from subprocess import (
    run,
    CalledProcessError,
    PIPE,
    check_output,
)
from collections import defaultdict
from charms.reactive import (
    when,
    when_not,
    set_flag,
    when_not_all,
    hook,
    clear_flag,
    when_any,
    data_changed,
)
from charms.reactive.relations import endpoint_from_flag
from charmhelpers.core.hookenv import (
    log,
    status_set,
    charm_dir,
)
from charmhelpers.core import unitdata, hookenv, host
from jujubigdata import utils
from charms.layer.resourcefactory import ResourceFactory
from charms.layer.k8shelpers import (
    delete_resources_by_label,
    get_label_values_per_deployer,
    add_label_to_resource,
    get_worker_node_ips,
    resource_owner,
    get_resource_by_file,
)


# Add kubectl to PATH
os.environ['PATH'] += os.pathsep + os.path.join(os.sep, 'snap', 'bin')
config = hookenv.config()
deployer = os.environ['JUJU_UNIT_NAME'].split('/')[0]


@when_not('kube-host.available')
def wait_for_k8s():
    status_set('blocked', 'Waiting for relation to Kubernetes-master')
    clear_flag('kubernetes.ready')


@when('kube-host.available')
@when_not('kubernetes.ready')
def check_master_ready(kube):
    if len(master_services_down()) == 0 and all_kube_system_pods_running():
        status_set('active', 'Ready')
        set_flag('kubernetes.ready')
    else:
        status_set('waiting', 'Waiting for Kubernetes master to be ready')


@when_not('deployer.installed')
def install_deployer():
    # Create user and configuration dir
    distconfig = utils.DistConfig(filename=charm_dir() + '/files/setup.yaml')
    distconfig.add_users()
    distconfig.add_dirs()
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


@when('endpoint.kubernetes-deployer.resources-changed',
      'kube-host.available',
      'kubernetes.ready',
      'leadership.is_leader')
def new_resource_request(dep, kube):
    status_set('active', 'Processing resource requests')
    configure_namespace()
    requests = dep.get_resource_requests()
    # Remove all config files since we are recreating them from the new requests
    clean_deployer_config(['resources'])
    # Store all uuids in the kv store so we can check later in the cleanup handler 
    # which are still in use (= still have a relation with the deployer)
    unitdata.kv().set('used_apps', list(requests.keys()))
    error_states = {}
    for uuid in requests:
        resource_id = 0
        for resource in requests[uuid]['requests']:
            # Check if there is a naming conflict in the namespace
            if resource_name_duplicate(resource, uuid):
                error_states[uuid] = {'error': 'Duplicate name for resource: '
                                               + resource['metadata']['name']}
                log('Duplicate name for resource: ' + resource['metadata']['name'])
                continue
            prepared_request = {
                'uuid': uuid,
                'resource': resource,
                'namespace': config.get('namespace').rstrip(),
                'unique_id': resource_id,
                'model_uuid': requests[uuid]['model_uuid'],
                'juju_unit': requests[uuid]['juju_unit'],
            }
            resource_id += 1
            pre_resource = ResourceFactory.create_resource('preparedresource', prepared_request)
            pre_resource.write_resource_file()
            if not pre_resource.create_resource():
                error_states[uuid] = {'error': 'Could not create requested resources.'}
    # Save the error states so update_status_info handler can report them
    unitdata.kv().set('error-states', error_states)
    if error_states:
        status_set('active', 'Could not create requested resources, check the deployer log for more details.')
    else:
        status_set('active', 'Ready')
    set_flag('resources.created')
    clear_flag('endpoint.kubernetes-deployer.resources-changed')


@when('endpoint.kubernetes-deployer.available',
      'kube-host.available',
      'kubernetes.ready',
      'leadership.is_leader')
@when_not('endpoint.kubernetes-deployer.resources-changed')
def update_status_info():
    endpoint = endpoint_from_flag('endpoint.kubernetes-deployer.available')
    status = check_predefined_resources()
    error_states = unitdata.kv().get('error-states', {})
    status.update(error_states)
    worker_ips = get_worker_node_ips()
    # Only report if the status has changed
    if (data_changed('status-info', status) 
        or data_changed('worker-ips', worker_ips)):
        endpoint.send_status(status)
        endpoint.send_worker_ips(worker_ips)
        

"""
CLEANUP STATES
"""


@when('resources.created',
      'leadership.is_leader')
def cleanup():
    # Iterate over all resources with label from this deployer
    # Remove all which are not needed anymore
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


@when('deployer.installed',
      'config.changed.isolated',
      'leadership.is_leader')
def create_policies():
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
            'uuid': {...},
            ...
        }
    """
    result = {}
    path = unitdata.kv().get('deployer_path') + '/resources'
    for file in os.listdir(path):
        # Resource files have the following naming rule: uuid-resource_id.yaml
        # We only need the uuid so the requesting charm can identify the resource.
        uuid = file.rsplit('-', 1)[0]
        if uuid not in result:
            result[uuid] = []
        result[uuid].append(get_resource_by_file("{}/{}".format(path, file)))
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


# Using master_services_down and all_kube_system_pods_running
# until a subordinate interface is available
def master_services_down():
    """Ensure master services are up and running.
    Return: list of failing services"""
    services = ['kube-apiserver',
                'kube-controller-manager',
                'kube-scheduler']
    failing_services = []
    for service in services:
        daemon = 'snap.{}.daemon'.format(service)
        if not host.service_running(daemon):
            failing_services.append(service)
    return failing_services


def all_kube_system_pods_running():
    ''' Check pod status in the kube-system namespace. Returns True if all
    pods are running, False otherwise. '''
    cmd = ['kubectl', 'get', 'po', '-n', 'kube-system', '-o', 'json']

    try:
        output = check_output(cmd).decode('utf-8')
    except CalledProcessError:
        hookenv.log('failed to get kube-system pod status')
        return False

    result = json.loads(output)
    for pod in result['items']:
        status = pod['status']['phase']
        # Evicted nodes should re-spawn
        if status != 'Running' and \
           pod['status'].get('reason', '') != 'Evicted':
            return False

    return True
