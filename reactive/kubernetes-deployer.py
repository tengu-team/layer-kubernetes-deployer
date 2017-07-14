#!/usr/bin/env python3
import os
import json
import random
from subprocess import call, check_output, check_call, CalledProcessError
from charms.reactive import when, when_not, remove_state, set_state
from charmhelpers.core.hookenv import log, is_leader, status_set
from charmhelpers.core.templating import render
from charmhelpers.core import unitdata


@when('docker-image-host.available', 'kubernetes-master.components.started')
def launch(relation):
    if is_leader():
        container_requests = relation.container_requests
        log(container_requests)
        running_containers = {}
        unit = ""
        unit_list = []
        for container_request in container_requests:
            unit = container_request['unit'].split('/')[0]
            unit_list.append(container_request['unit'])
        if unit:
            deployment_context = {
                'name': unit + '-deployment',
                'replicas': len(container_requests),
                'uname': unit,
                'image': container_request.get('image'),
                'env_vars': {'units': ' '.join(unit_list)}
            }
            # Add env vars if needed
            if 'env' in container_request:
                deployment_context['env_vars'] = {**deployment_context['env_vars'],
                                                  **container_request['env']}
            # Create secret if needed
            if is_secret_image(container_request):
                secret_name = get_secret(container_request)
                if not secret_name:
                    status_set('blocked',
                               'Secret for ' + unit + ' could not be created')
                    return
                deployment_context['imagesecret'] = secret_name
            # Create deployment and service if needed
            render(source='deployment.tmpl',
                   target='/home/kubedeployer/.config/deployments/' + unit + '.yaml',
                   context=deployment_context)

            if 'ports' in container_request and not service_exists(unit + '-service'):
                service_context = {
                    'name': unit + '-service',
                    'uname': unit,
                    'ports': get_ports_context(container_request)
                }
                render(source='service.tmpl',
                       target='/home/kubedeployer/.config/services/' + unit + '.yaml',
                       context=service_context)
                call(['kubectl', 'create', '-f',
                      '/home/kubedeployer/.config/services/' + unit + '.yaml'])
            call(['kubectl', 'apply', '-f',
                  '/home/kubedeployer/.config/deployments/' + unit + '.yaml'])
            # Check if deployed pods are ready
            errors = unitdata.kv().get('deployer-errors', {})
            if not check_pods(unit):
                error_msg = get_pod_error_message(unit)
                if error_msg:
                    errors[unit] = error_msg
                    unitdata.kv().set('deployer-errors', errors)
                    set_state('kubernetes-deployer.error')
                    return
            errors.pop(unit, None)
            unitdata.kv().set('deployer-errors', errors)
            # Return running container info
            running = get_running_containers(unit)
            for u in unit_list:
                running_containers[u] = running
            relation.send_running_containers(running_containers)
            status_set('active', 'Kubernetes master running')


@when('docker-image-host.broken')
@when_not('docker-image-host.available')
def remove_images(relation):
    container_requests = relation.container_requests
    log(container_requests)
    for container_request in container_requests:
        unit = container_request['unit'].split('/')[0]
    remove_deployment(unit)
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


def remove_deployment(unit):
    """Deletes unit deployment and service.and

    Args:
        unit (str): concatenation of unit, - and image (replace all / with -)
    """
    service_path = '/home/kubedeployer/.config/services/' + unit + '.yaml'
    deployement_path = '/home/kubedeployer/.config/deployments/' + unit + '.yaml'
    if os.path.exists(service_path):
        call(['kubectl', 'delete', '-f', service_path])
        os.remove(service_path)
    if os.path.exists(deployement_path):
        call(['kubectl', 'delete', '-f', deployement_path])
        os.remove(deployement_path)


def get_running_containers(unit):
    ''' Returns service host and port information about a unit deployment.
    Returns only a worker host if no service exists.

    Args:
        unit (str): unit
    Returns:
        dict {
                'host': '0.0.0.0',
                'ports': {
                          '8080': 30000
                         }
             }
    '''
    config = {'host': get_random_node_ip()}
    try:
        service_info = check_output(['kubectl',
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
    """
    nodes = check_output(['kubectl',
                         'get',
                         'nodes',
                         '-o',
                         'jsonpath="{.items[0].status.addresses[*].address}"'
                         ]).decode('utf-8')
    nodes = nodes.replace('"', '')
    return random.choise(nodes.split(' '))


def service_exists(service):
    """Check if a service is active.

    Args:
        service (str): name of service
    Returns:
        True | False
    """
    try:
        check_call(['kubectl', 'get', 'service', service])
    except CalledProcessError:
        return False
    return True


def secret_exists(secret):
    """Check if a secret exists.

    Args:
        secret (str): name of the secret
    Returns:
        True | False
    """
    try:
        check_call(['kubectl', 'get', 'secret', secret])
    except CalledProcessError:
        return False
    return True


def create_secret(container):
    """Creates a secret for a unit.

    Args:
        container (dict): container_request
    Returns:
        Name of the secret
    """
    unit = container['unit'].split('/')[0]
    try:
        check_call(['kubectl',
                    'create',
                    'secret',
                    'docker-registry',
                    unit + '-secret',
                    '--docker-server=' + container['docker-registry'],
                    '--docker-username=' + container['username'],
                    '--docker-password=' + container['password'],
                    '--docker-email=bogus@examplebogus.be'])
    except CalledProcessError:
        return ''
    return unit + 'secret'


def is_secret_image(container):
    """Checks if all information is available for secret image.
    If all are present, assume a secret is needed.

    Args:
        container (dict): container_request
    Returns:
        True, if not all information is present
    """
    required_fields = ['username', 'password', 'docker-registry']
    for field in required_fields:
        if field not in container or bool(container[field].isspace()):
            return False
    return True


def get_secret(container):
    """Checks if unit secret exists.
    If not, create a new secret.
    If yes, check if info is updated.

    Args:
        container (dict): container_request
    Returns:
        Name of the secret
    """
    unit = container['unit'].split('/')[0]
    secret_info = {'username': container['username'],
                   'password': container['password'],
                   'docker-registry': container['docker-registry'],
                   'docker-email': container['docker-email']}
    if secret_exists(unit + '-secret'):
        if unitdata.kv().get(unit) == secret_info:
            return unit + '-secret'
        else:
            call(['kubectl', 'delete', 'service', unit + '-secret'])
    unitdata.kv().set(unit, secret_info)
    return create_secret(container)


def check_pods(unit):
    """Checks if all pods from a service are running.

    Args:
        unit (str): unit
    Returns:
        True | False
    """
    deployment_status = check_output(['kubectl',
                                      'get',
                                      'pods',
                                      '--selector=pod-is-for=' + unit,
                                      '--output=jsonpath={.items[*].status.containerStatuses[*].ready}'
                                      ]).decode('utf-8')
    pods_ready = deployment_status.split(' ')
    for pod in pods_ready:
        if pod == 'false':
            return False
    return True


def get_pod_error_message(unit):
    """Return the first encountered error state of a pod within a service.

    Args:
        unit (str): unit
    Returns:
        None | dict

    """
    deployment_status = json.loads(check_output(['kubectl',
                                                 'get',
                                                 'pods',
                                                 '--selector=pod-is-for=' + unit,
                                                 '-o',
                                                 'json'
                                                 ]).decode('utf-8'))
    if deployment_status:
        for item in deployment_status['items']:
            for status in item['status']['containerStatuses']:
                if status['ready'] is False:
                    return status['state']
    return None
