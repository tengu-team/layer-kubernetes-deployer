import os
import json
import random
from subprocess import call, check_output, check_call, CalledProcessError
from charmhelpers.core.hookenv import log


'''
GENERAL HELPER METHODS
'''


def create_resources(path):
    """Create Kubernetes resources based on generated config files.
    """
    try:
        call(['kubectl', 'apply', '-R', '-f', path])
    except CalledProcessError as e:
        log('Could not create, modify resources')
        log(e)


def delete_resources_by_label(namespace, resources, label):  # resources is type list !
    try:
        check_call(['kubectl',
                    'delete',
                    ','.join(resources),
                    '--namespace',
                    namespace,
                    '--selector=' + label])
    except CalledProcessError as e:
        log(e)


def delete_resource_by_name(namespace, resource, name):
    try:
        check_call(['kubectl',
                    'delete',
                    resource,
                    '--namespace',
                    namespace,
                    name])
    except CalledProcessError as e:
        log(e)


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
                                     unit,  # Full name?
                                     '-o',
                                     'json']).decode('utf-8')
        service = json.loads(service_info)
        ports = {}
        for port in service['spec']['ports']:
            if 'nodePort' in port:
                ports[port['port']] = port['nodePort']
        config['ports'] = ports
    except CalledProcessError:
        pass
    return config


def get_label_values_per_deployer(namespace, label, deployerlabel):
    """Return a list with all distinct label values in this namespace.
    
    Args:
        namespace (str): namespace to search in
        label (str): label
        deployerlabel (str): deployer selector
    return:
        list with distinct values
    """
    unique_values = set()
    try:
        values = check_output(['kubectl', 'get', 'all', '--namespace', namespace, '--selector=' + deployerlabel, '-o',
                               'jsonpath="{.items[*].metadata.labels[\'' + label + '\']}']).decode('utf-8')
        values = values.replace('"', '')
        for value in values.split(' '):
            unique_values.add(value)
    except CalledProcessError:
        pass
    return list(unique_values)


'''
NAMESPACE HELPER METHODS
'''


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


def create_namespace_by_file(path):
    """Create a namespace via file

    Args:
        path (str): path to namespace yaml
    Returns:
        True | False on success or failure
    """
    try:
        check_call(['kubectl', 'create', '-f', path])
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
                         'pods,services',
                         '--namespace',
                         namespace]):
        log('No resources found for namespace ' + namespace + ' ... deleting')
        call(['kubectl', 'delete', 'namespace', namespace])
    else:
        log('Resources found for namespace ' + namespace + ', not deleting')


'''
SECRET HELPER METHODS
'''


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


def delete_secret(secret, namespace):
    """Check if a secret exists.

        Args:
            secret (str): name of the secret
            namespace (str): namespace of the secret
    """
    try:
        call(['kubectl', '--namespace', namespace, 'delete', 'secret', secret])
    except CalledProcessError as e:
        log(e)


def create_secret(namespace, name, username, password, juju_app_label, deployer_label,
                  dockerregistry='https://index.docker.io/v1/'):
    """Creates a secret for a unit.

    Args:        
        namespace (str): namespace of secret
        name (str): name of secret
        username (str): docker username
        password (str): docker password
        juju_app_label (str): juju app label
        deployer_label (str): deployer label
        dockerregistry (str): docker registry
    Returns:
        Name of the secret
    """
    try:
        output = check_output(['kubectl',
                               '--namespace',
                               namespace,
                               'create',
                               'secret',
                               'docker-registry',
                               name,
                               '--docker-server=' + dockerregistry,
                               '--docker-username=' + username,
                               '--docker-password=' + password,
                               '--docker-email=bogus@examplebogus.be'])
        call(['kubectl', '--namespace', namespace, 'label', 'secrets', name, juju_app_label])
        call(['kubectl', '--namespace', namespace, 'label', 'secrets', name, deployer_label])
    except CalledProcessError as e:
        log(e)


