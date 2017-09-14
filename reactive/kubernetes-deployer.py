#!/usr/bin/env python3
import os
import shutil
from collections import defaultdict
from charms.reactive import when, when_not, remove_state, set_state, when_not_all, hook
from charmhelpers.core.hookenv import log, is_leader, status_set
from charmhelpers.core import unitdata, hookenv
from charms.layer.resourcefactory import ResourceFactory
from charms.layer.k8shelpers import (
    get_running_containers,
    delete_resources_by_label,
    get_label_values_per_deployer,
    add_label_to_resource
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
    dirs = ['deployments', 'services', 'secrets', 'headless-services']
    for d in dirs:
        if not os.path.exists(deployer_path + '/' + d):
            os.makedirs(deployer_path + '/' + d)
    # Setup the default namespace
    add_label_to_resource('default', namespace_selector + '=default', 'namespace', 'default', True)
    set_state('deployer.installed')


@when('kubernetes-deployer.available', 'kube-host.available')
def create_resources(relation, kube):
    # Check if this is the leader
    if not is_leader():
        return
    # Ensure namespace exists
    configure_namespace()
    # Check headless service requests
    headless_service_requests = relation.headless_service_requests
    headless_services_running = {}  # Contains all headless services info
    if headless_service_requests:
        clean_deployer_config(['headless-services'])
        headless_services_running = configure_headless_services(headless_service_requests)
    # Return all created services
    relation.send_services(headless_services_running)
    status_set('active', 'Ready')
    set_state('resources.created')


@when('docker-image-host.available', 'kube-host.available')
def launch(relation, kube):
    # Received at least one container request
    # First check if this is the leader
    if not is_leader():
        return
    # Make sure the namespace exists
    configure_namespace()
    # Delete all config files from this deployer
    clean_deployer_config()
    # Create all config files from container_requests
    application_units = defaultdict(list)  # dict with unit names per application
    application_names = {}  # Dict with all applications with their request
    container_requests = relation.container_requests
    for container_request in container_requests:
        if container_request:
            unit = container_request['unit'].split('/')[0]
            application_names[unit] = container_request
            application_units[unit].append(container_request['unit'])
    # Per app create resource files
    service_names = {}  # Save all service names so we can query them later
    for app, request in application_names.items():
        # Create secrets
        secret = configure_secret(app, request, application_names)
        # Create deployments files
        deployment = configure_deployment(app, request, application_units[app], secret)
        # Create services files
        service = configure_service(app, request)
        if service:
            service_names[service.name()] = app
        else:
            service_names[app] = app  # prettify?
        # Create resources
        deployment.create_resource()  # Will implicitly call apply all resources
    relation.send_running_containers(get_running_info(service_names))
    used_apps = unitdata.kv().get('used_apps', [])
    unitdata.kv().set('used_apps', list(set(used_apps) | application_names.keys()))
    status_set('active', 'Ready')
    set_state('deployments.created')


"""
CLEANUP STATES
"""


@when_not_all('kubernetes-deployer.available', 'docker-image-host.available')
def ensure_cleanup():
    set_state('resources.created')
    set_state('deployments.created')


@when('deployments.created', 'resources.created', 'kube-host.available')
def cleanup(kube):
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
                                      ['all'],
                                      unitdata.kv().get('juju_app_selector') + '=' + app)
    unitdata.kv().set('used_apps', [])

    if config.changed('namespace') and config.previous('namespace').rstrip():
        log('Checking if previous namespace still has resources, if not delete namespace (' +
            config.previous('namespace').rstrip() + ')')
        namespace = ResourceFactory.create_resource('namespace', {'name': config.previous('namespace').rstrip()})
        namespace.delete_resource()

    remove_state('resources.created')
    remove_state('deployments.created')


@hook('stop')
def clean_deployer_configs():
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
    if not config['isolated'] :
        policy.delete_resource()
        return
    if config.changed('namespace') and config.previous('namespace').rstrip():
        policy.delete_resource()
    policy.write_resource_file()
    policy.create_resource()


def clean_deployer_config(resources=None):
    """Remove all resource files from this deployer. If no resources are given,
    remove deployments, services and secrets
    """
    if resources is None:
        resources = ['deployments', 'services', 'secrets']
    for resource in resources:
        path = unitdata.kv().get('deployer_path') + '/' + resource
        shutil.rmtree(path)
        os.mkdir(path)


def is_secret_image(container):
    """Checks if all information is available for secret image.
    If all are present, assume a secret is needed.

    Args:
        container (dict): container_request
    Returns:
        True, if all information is present
    """
    required_fields = ['username', 'password', 'docker-registry']
    for field in required_fields:
        if field not in container or bool(container[field].isspace()):
            return False
    return True


def get_ports_context(container):
    """Decides which ports will be exposed.

    Args:
        container (dict): container_request
    Returns:
        An array with ports.
    """
    ports = []
    counter = 1
    if 'ports' in container and container['ports']:
        for key, value in container['ports'].items():
            if value != "":
                ports.append({key: key + '-' + str(counter)})
                counter += 1
    return ports


def configure_namespace():
    namespace = ResourceFactory.create_resource('namespace', {'name': config.get('namespace', 'default').rstrip(),
                                                              'deployer': deployer})
    namespace.write_resource_file()
    namespace.create_resource()
    # Check if config.namespace changed
    if config.changed('namespace') and config.previous('namespace').rstrip():
        # Remove all resources from previous namespace created by this deployer
        prev_namespace = ResourceFactory.create_resource('namespace',
                                                         {'name': config.previous('namespace').rstrip(),
                                                          'deployer': deployer})
        prev_namespace.delete_namespace_resources()


def configure_secret(app, request, application_names):
    secret = None
    # Check if request has secret info
    if is_secret_image(application_names[app]):
        secret = ResourceFactory.create_resource('secret', {'username': request['username'],
                                                            'password': request['password'],
                                                            'docker-registry': request['docker-registry'],
                                                            'deployer': deployer,
                                                            'app': app,
                                                            'namespace': config.get('namespace', 'default').rstrip()})
        secret.create_resource()
    return secret


def configure_deployment(app, request, application_units, secret):
    deployment_request = {
        'name': app,
        'replicas': len(application_units),
        'image': request['image'],
        'namespace': config.get('namespace').rstrip(),
        'rolling': config.get('rolling-updates'),
        'env_vars': {'units': ' '.join(application_units)}
    }
    if 'env' in request:
        deployment_request['env_vars'] = {**deployment_request['env_vars'],
                                          **request['env']}
        sorted_env_keys = sorted(deployment_request['env_vars'].keys())  # Sort for the same pod hash
        deployment_request['env_order'] = sorted_env_keys
    if secret:
        deployment_request['secret'] = secret.name()
    deployment = ResourceFactory.create_resource('deployment', deployment_request)
    deployment.write_resource_file()
    return deployment


def configure_service(app, request):
    service = None
    if 'ports' in request:
        service_request = {
            'name': app,
            'ports': get_ports_context(request),
            'namespace': config.get('namespace', 'default').rstrip()
        }
        service = ResourceFactory.create_resource('service', service_request)
        service.write_resource_file()
    return service


def get_running_info(service_names):
    """Returns service info.
    
    Args:
        service_names (dict): {'service_name': 'app_name'}
    Returns:
        See k8shelpers.get_running_containers()
    """
    running_containers = {}
    namespace = config.get('namespace').rstrip()
    for service, app in service_names.items():
        running_containers[app] = get_running_containers(service, namespace)
    return running_containers


def configure_headless_services(requests):
    """Create requested headless services.
    
    Args:
        requests (list): list with requests
    Return:
        running services (dict)
    """
    application_names = {}
    service_names = {}
    for request in requests:
        application_names[request['unit'].split('/')[0]] = request
    for app, hs_req in application_names.items():
        request = {
            'name': app,
            'namespace': config.get('namespace').rstrip(),
            'port': hs_req['port'],
            'ips': hs_req['ips']
        }
        headless_service = ResourceFactory.create_resource('headless-service', request)
        headless_service.write_resource_file()
        service_names[headless_service.name()] = app
    headless_service.create_resource()  # One call to create resources will create all new / modified services
    used_apps = unitdata.kv().get('used_apps', [])
    unitdata.kv().set('used_apps', list(set(used_apps) | application_names.keys()))
    return get_running_info(service_names)
