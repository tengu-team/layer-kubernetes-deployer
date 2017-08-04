#!/usr/bin/env python3
import os
import json
import random
from collections import defaultdict
from subprocess import call, check_output, check_call, CalledProcessError
from charms.reactive import when, when_not, remove_state, set_state
from charmhelpers.core.hookenv import log, is_leader, status_set
from charmhelpers.core.templating import render
from charmhelpers.core import unitdata, hookenv

# Add kubectl to PATH
os.environ['PATH'] += os.pathsep + os.path.join(os.sep, 'snap', 'bin')
# Deployer config files path
deployer_path = '/home/kubedeployer/.config/kubedeployers'
deployer_configs = deployer_path + '/' + os.environ['JUJU_UNIT_NAME'].replace('/', '-')
# Label selector
selector = 'resource-for'


@when_not('deployer.installed')
def install_deployer():
    log('Setting up deployer dirs in: ' + deployer_configs)
    if not os.path.exists('/home/kubedeployer/.config/kubedeployers/namespaces'):
        os.makedirs('/home/kubedeployer/.config/kubedeployers/namespaces')
    dirs = ['deployments', 'services', 'secrets']
    for d in dirs:
        if not os.path.exists(deployer_configs + '/' + d):
            os.makedirs(deployer_configs + '/' + d)
    set_state('deployer.installed')


@when_not('kube-host.available')
def wait_for_k8s():
    status_set('blocked', 'Waiting for relation to Kubernetes-master')


@when('kube-host.available')
@when_not('kube-deployer.connected')
def set_active(kube):
    status_set('active', 'Ready')


@when('docker-image-host.available', 'kube-host.available')
def launch(relation, kube):
    if is_leader():
        config = hookenv.config()
        container_requests = relation.container_requests
        if not container_requests:
            return
        log(container_requests)
        running_containers = {}
        application_units = defaultdict(list)
        application_names = set()  # Set with all distinct application names
        for container_request in container_requests:
            unit = container_request['unit'].split('/')[0]
            application_names.add(unit)
            application_units[unit].append(container_request['unit'])
        namespace = config.get('namespace', 'default')
        update_namespace(os.environ['JUJU_UNIT_NAME'])
        for app in application_names:
            # Create the deployment
            launch_deployment(container_request, len(application_units[app]), application_units[app], namespace)
            # Check if a service is needed, create if needed
            launch_service(container_request, namespace)
            # Tell k8s to create resources
            create_resources()
            # Check if deployed pods are ready
            errors = unitdata.kv().get('deployer-errors', {})
            if not check_pods(app, namespace):
                error_msg = get_pod_error_message(app, namespace)
                if error_msg:
                    errors[app] = error_msg
                    unitdata.kv().set('deployer-errors', errors)
                    set_state('kubernetes-deployer.error')
                    return
            errors.pop(app, None)
            unitdata.kv().set('deployer-errors', errors)
            # Return running container info
            running_containers[app] = get_running_containers(app, namespace)
        relation.send_running_containers(running_containers)
        remove_old_deployments(application_names, namespace)
        status_set('active', 'Kubernetes master running')
        set_state('kubernetes-deployer.cleanup')


@when('kubernetes-deployer.cleanup')
def cleanup():
    log('Remove unused namespaces')
    namespaces = json.loads(check_output(['kubectl',
                                          'get',
                                          'namespaces',
                                          '--selector=created-by=deployer',
                                          '-o',
                                          'json']).decode('utf-8'))
    for ns in namespaces['items']:
        delete_namespace(ns['metadata']['name'])


@when('docker-image-host.broken')
@when_not('docker-image-host.available')
def remove_images(relation):
    container_requests = relation.container_requests
    namespace = hookenv.config().get('namespace', 'default')
    log(container_requests)
    for container_request in container_requests:
        unit = container_request['unit'].split('/')[0]
    remove_deployment(unit, namespace)
    if namespace is not 'default':
        delete_namespace(namespace)
    resources = [name for name in os.listdir(deployer_configs) if os.path.isdir(deployer_configs + '/' + name)]
    for resource in resources:
        for the_file in os.listdir(deployer_configs + '/' + resource):
            file_path = os.path.join(deployer_configs + '/' + resource, the_file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(e)
    remove_state('docker-image-host.broken')
    status_set('active', 'Kubernetes master running')


@when('kubernetes-deployer.error')
def report_errors():
    errors = unitdata.kv().get('deployer-errors')
    error_message = 'Error deploying the following application(s):'
    for key in errors:
        error_message += '\n' + key + ' --> ' + json.dumps(errors[key])
    status_set('active', error_message)
    remove_state('kubernetes-deployer.error')


def launch_deployment(container_request, nr_pods, unit_list, namespace):
    """Creates a deployment file and generates a docker secret if needed.
    
    Args:
        container_request (dict): container_request
        nr_pods (int): number of pods
        unit_list (list): list with unit names
        namespace (str): namespace for deployment
    """
    unit = container_request['unit'].split('/')[0]
    deployment_context = {
        'name': unit + '-deployment',
        'replicas': nr_pods,
        'uname': unit,
        'image': container_request.get('image'),
        'namespace': namespace,
        'rolling': hookenv.config().get('rolling-updates'),
        'env_vars': {'units': ' '.join(unit_list)},
        'selector': selector
    }
    # Add env vars if needed
    if 'env' in container_request:
        deployment_context['env_vars'] = {**deployment_context['env_vars'],
                                          **container_request['env']}
        sorted_env_keys = sorted(deployment_context['env_vars'].keys())
        deployment_context['env_order'] = sorted_env_keys
    # Create secret if needed
    if is_secret_image(container_request):
        secret_name = get_secret(container_request, namespace)
        if not secret_name:
            status_set('blocked',
                       'Secret for ' + unit + ' could not be created')
            return
        deployment_context['imagesecret'] = secret_name
    # Create deployment and service if needed
    render(source='deployment.tmpl',
           target=deployer_configs + '/deployments/' + unit + '.yaml',
           context=deployment_context)


def launch_service(container_request, namespace):
    """Create a service configuration file if a ports config is found.
    
    Args:
        container_request (dict): container_request
        namespace (str): namespace for service
    """
    unit = container_request['unit'].split('/')[0]
    if 'ports' in container_request and not service_exists(unit + '-service', namespace):
        service_context = {
            'name': unit + '-service',
            'uname': unit,
            'ports': get_ports_context(container_request),
            'namespace': namespace,
            'selector': selector
        }
        render(source='service.tmpl',
               target=deployer_configs + '/services/' + unit + '.yaml',
               context=service_context)


def update_namespace(unit):
    """Create and/or update the namespace configured for this deployer.
    
    Args:
        unit (str): juju unit name of the deployer
    """
    config = hookenv.config()
    # Create namespace if needed
    namespace = config.get('namespace', 'default')
    if not namespace_exists(namespace):
        create_namespace(namespace)
        # Check if new namespace
        if config.changed('namespace'):
            resources = ['deployments', 'services', 'secrets']
            for resource in resources:
                delete_resource_label(resource, selector + '=' + unit, config.previous('namespace'))


def create_resources():
    """Create Kubernetes resources based on generated config files.
    """
    try:
        call(['kubectl', 'apply', '-R', '-f', deployer_configs + '/'])
    except CalledProcessError as e:
        log('Could not create, modify resources')
        log(e)


def remove_old_deployments(app_names, namespace):
    """Delete config files that are no longer in use.
    Delete resources that these files represent if they are still active.
    
    Args:
        app_names (set): list with active connected applications
        namespace (str): namespace to clean up
    """
    resources = [name for name in os.listdir(deployer_configs) if os.path.isdir(deployer_configs + '/' + name)]
    apps_to_delete = set()
    for resource in resources:
        for file_name in os.listdir(deployer_configs + '/' + resource):
            file_no_extension = file_name.rstrip(file_name)
            if file_no_extension not in app_names:
                apps_to_delete.add(file_no_extension)
    for app in apps_to_delete:
        for resource in resources:
            delete_resource_label(resource, selector + '=' + app, namespace)
            path = deployer_configs + '/' + resource + '/' + app + '.yaml'
            if os.path.exists(path):
                os.remove(path)


def get_ports_context(container):
    """Decides which ports will be exposed.

    Args:
        container (dict): container_request
    Returns:
        An array with ports.
    """
    ports = []
    counter = 1
    if 'ports' in container:
        for key, value in container['ports'].items():
            if value != "":
                ports.append({key: key + '-' + str(counter)})
                counter += 1
    return ports


def remove_deployment(unit, namespace):
    """Deletes unit deployment and service.

    Args:
        unit (str): concatenation of unit, - and image (replace all / with -)
        namespace (str): namespace of the deployment
    """
    service_path = deployer_configs + '/services/' + unit + '.yaml'
    deployement_path = deployer_configs + '/deployments/' + unit + '.yaml'
    if os.path.exists(service_path):
        call(['kubectl',
              '--namespace', namespace,
              'delete', '-f', service_path])
        os.remove(service_path)
    if os.path.exists(deployement_path):
        call(['kubectl',
              '--namespace', namespace,
              'delete', '-f', deployement_path])
        os.remove(deployement_path)


def get_running_containers(unit, namespace):
    """ Returns service host and port information about a unit deployment.
    Returns only a worker host if no service exists.

    Args:
        unit (str): unit
        namespace (str): namespace of service
    Returns:
        dict {
                'host': '0.0.0.0',
                'ports': {
                          '8080': 30000
                         }
             }
    """
    config = {'host': get_random_node_ip()}
    try:
        service_info = check_output(['kubectl',
                                     '--namespace', namespace,
                                     'get',
                                     'service',
                                     unit + '-service',
                                     '-o',
                                     'json']).decode('utf-8')
        service = json.loads(service_info)
        ports = {}
        for port in service['spec']['ports']:
            ports[port['port']] = port['nodePort']
        config['ports'] = ports
    except CalledProcessError:
        pass
    return config


def get_random_node_ip():
    """Returns a random kubernetes-worker node address.
       Can be an ip adress or hostname

    Returns:
        str
    """
    nodes = check_output(['kubectl',
                          'get',
                          'nodes',
                          '-o',
                          'jsonpath="{.items[0].status.addresses[*].address}"'
                          ]).decode('utf-8')
    nodes = nodes.replace('"', '')
    return random.choice(nodes.split(' '))


def service_exists(service, namespace):
    """Check if a service is exists.

    Args:
        service (str): name of service
        namespace (str): namespace of the service
    Returns:
        True | False
    """
    try:
        check_call(['kubectl',
                    '--namespace', namespace,
                    'get', 'service', service])
    except CalledProcessError:
        return False
    return True


def secret_exists(secret, namespace):
    """Check if a secret exists.

    Args:
        secret (str): name of the secret
        namespace (str): namespace of the secret
    Returns:
        True | False
    """
    try:
        check_call(['kubectl',
                    '--namespace', namespace,
                    'get', 'secret', secret])
    except CalledProcessError:
        return False
    return True


def namespace_exists(namespace):
    """Check if a namespace exists.

    Args:
        namespace (str): name of the namespace
    Returns:
         True | False
    """
    try:
        check_call(['kubectl', 'get', 'namespace', namespace])
    except CalledProcessError:
        return False
    return True


def create_namespace(namespace):
    """Create a namespace

    Args:
        namespace (str): name of the namespace
    Returns:
        True | False on success or failure
    """
    render(source='namespace.tmpl',
           target=deployer_path + '/namespaces/' + namespace + '.yaml',
           context={'namespace': namespace})

    try:
        check_call(['kubectl',
                    'create',
                    '-f',
                    deployer_path + '/namespaces/' + namespace + '.yaml'])
    except CalledProcessError:
        return False
    return True


def delete_namespace(namespace):
    """Delete a namespace if no pods are running in the namespace

     Args:
         namespace (str): name of the namespace
    """
    if not check_output(['kubectl',
                         'get',
                         'pods',
                         '--namespace',
                         namespace]):
        log('No resources found for namespace ' + namespace + ' ... deleting')
        call(['kubectl', 'delete', 'namespace', namespace])
        if os.path.exists(deployer_path + '/namespaces/' + namespace + '.yaml'):
            os.remove(deployer_path + '/namespaces/' + namespace + '.yaml')
    else:
        log('Resources found for namespace ' + namespace + ', not deleting')


def create_secret(container, namespace):
    """Creates a secret for a unit.

    Args:
        container (dict): container_request
        namespace (str): namespace of secret
    Returns:
        Name of the secret
    """
    unit = container['unit'].split('/')[0]
    try:
        output = check_output(['kubectl',
                               '--namespace',
                               namespace,
                               'create',
                               'secret',
                               'docker-registry',
                               unit + '-secret',
                               '--docker-server=' + container['docker-registry'],
                               '--docker-username=' + container['username'],
                               '--docker-password=' + container['password'],
                               '--docker-email=bogus@examplebogus.be'])
        call(['kubectl', '--namespace', namespace, 'label',
              'secrets', unit + '-secret', selector + '=' + unit])
    except CalledProcessError:
        return ''
    return unit + 'secret'


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


def get_secret(container, namespace):
    """Checks if unit secret exists.
    If not, create a new secret.
    If yes, check if info is updated.

    Args:
        container (dict): container_request
        namespace (str): namespace of secret
    Returns:
        Name of the secret
    """
    unit = container['unit'].split('/')[0]
    secret_info = {'username': container['username'],
                   'password': container['password'],
                   'docker-registry': container['docker-registry']}
    if secret_exists(unit + '-secret', namespace):
        if unitdata.kv().get(unit + '-secret') == secret_info:
            return unit + '-secret'
        else:
            call(['kubectl',
                  '--namespace', namespace,
                  'delete', 'service', unit + '-secret'])
    unitdata.kv().set(unit + '-secret', secret_info)
    return create_secret(container, namespace)


def check_pods(unit, namespace):
    """Checks if all pods from a service are running.

    Args:
        unit (str): unit
        namespace (str): namespace of service
    Returns:
        True | False
    """
    deployment_status = check_output(['kubectl',
                                      '--namespace', namespace,
                                      'get',
                                      'pods',
                                      '--selector=' + selector + '=' + unit,
                                      '--output=jsonpath={.items[*].status.containerStatuses[*].ready}'
                                      ]).decode('utf-8')
    pods_ready = deployment_status.split(' ')
    for pod in pods_ready:
        if pod == 'false':
            return False
    return True


def get_pod_error_message(unit, namespace):
    """Return the first encountered error state of a pod within a service.

    Args:
        unit (str): unit
        namespace (str): namespace of the pods
    Returns:
        None | dict

    """
    deployment_status = json.loads(check_output(['kubectl',
                                                 '--namespace', namespace,
                                                 'get',
                                                 'pods',
                                                 '--selector=' + selector + '=' + unit,
                                                 '-o',
                                                 'json'
                                                 ]).decode('utf-8'))
    if deployment_status:
        for item in deployment_status['items']:
            for status in item['status']['containerStatuses']:
                if status['ready'] is False:
                    return status['state']
    return None


def delete_resource_label(resource, label, namespace="default"):
    try:
        check_call(['kubectl',
                    'delete',
                    resource,
                    '--namespace',
                    namespace,
                    '--selector=' + label])
    except CalledProcessError as e:
        log(e)
