import os
import re
import yaml
import time
from . import k8shelpers as k8s
from charmhelpers.core.templating import render
from charmhelpers.core import unitdata
from charmhelpers.core.hookenv import log, config

config = config()


class ResourceFactory(object):
    @staticmethod
    def create_resource(resource_type, request):
        if resource_type == 'preparedresource':
            return PreparedResource(request)
        elif resource_type == 'namespace':
            return Namespace(request)
        elif resource_type == 'network-policy':
            return NetworkPolicy(request)


class Resource(object):
    def __init__(self, request=None):
        self.request = request
        self.deployer_name = os.environ['JUJU_UNIT_NAME'].split('/')[0]
        self.juju_app_selector = unitdata.kv().get('juju_app_selector')
        self.deployer_selector = unitdata.kv().get('deployer_selector')
        self.namespace_selector = unitdata.kv().get('namespace_selector')
        self.deployers_path = unitdata.kv().get('deployers_path')  # Path to all deployer dirs
        self.deployer_path = unitdata.kv().get('deployer_path')  # Path to this deployer dir

    def create_resource(self):
        raise NotImplementedError()

    def delete_resource(self):
        raise NotImplementedError()

    def write_resource_file(self):
        raise NotImplementedError()

    def name(self):
        raise NotImplementedError()


class PreparedResource(Resource):
    """request = {
        'name': name of the juju unit requesting the resource,
        'resource': resource info,
        'namespace': namespace where to create resource,
        'unique_id': an id needed to generate unique file names, MUST be INT
    }
    request contains the full resource file in a dict
    """
    def write_resource_file(self):
        # Fill in namespace and labels
        # Check needed for valid metadata tag (if it even exists??)
        if 'metadata' not in self.request['resource']:
            self.request['resource']['metadata'] = {}
        self.request['resource']['metadata']['namespace'] = self.request['namespace']
        self.request['resource']['metadata']['name'] += '-' + self.request['name']
        if 'labels' not in self.request['resource']['metadata']:
            self.request['resource']['metadata']['labels'] = {}
        self.request['resource']['metadata']['labels'][self.juju_app_selector] = self.request['name']
        self.request['resource']['metadata']['labels'][self.deployer_selector] = self.deployer_name

        with open(self.deployer_path +
                  '/resources/' +
                  self.request['name'] +
                  '-' +
                  str(self.request['unique_id']) +
                  '.yaml', 'w+') as f:
            yaml.dump(self.request['resource'], f)

    def delete_resource(self):
        # WARNING This will delete ALL resources requested from the juju unit
        unit_name = self.request['name'].split('/')  # Filter away the juju unit number
        for file in os.listdir(self.deployer_path + '/resources'):
            if re.match('^' + unit_name + '-(\d+)\.yaml'):
                k8s.delete_resource_by_file(self.deployer_path + '/resources/' + file)

    def name(self):
        return self.request['name']

    def create_resource(self):
        k8s.create_resources(self.deployer_path)


class NetworkPolicy(Resource):
    """request = {
        'namespace': namespace of the policy,
        'name': name of the policy
    }    
    """
    def write_resource_file(self):
        render(source='network-policy.tmpl',
               target=self.deployers_path + '/network-policies/' + self.request['name'] + '.yaml',
               context={
                   'name': self.request['name'],
                   'namespace': self.request['namespace'],
                   'namespace_selector': self.namespace_selector
               })

    def create_resource(self):
        if not k8s.networkpolicy_exists(self.request['namespace'], self.name()):
            k8s.create_resource_by_file(self.deployers_path + '/network-policies/' + self.request['name'] + '.yaml')

    def name(self):
        return self.request['name']

    def delete_resource(self):
        if not k8s.networkpolicy_exists(self.request['namespace'], self.name()):
            return
        k8s.delete_networkpolicy(self.request['namespace'], self.name())
        resource_path = self.deployers_path + '/network-policies/' + self.request['name'] + '.yaml'
        if os.path.exists(resource_path):
            os.remove(resource_path)


class Namespace(Resource):
    """request = {
        'name': name of the namespace,
        'deployer': name of the deployer
    }    
    """
    def write_resource_file(self):
        log('Writing resource file for namespace')
        namespace_context = {
            'namespace': self.request['name'],
            'deployer': self.request['deployer'],
            'deployer_selector': self.deployer_selector,
            'namespace_selector': self.namespace_selector
        }
        render(source='namespace.tmpl',
               target=self.deployers_path + '/namespaces/' + self.request['name'] + '.yaml',
               context=namespace_context)

    def name(self):
        return self.request['name']

    def create_resource(self):
        if not k8s.namespace_exists(self.request['name']):
            k8s.create_resource_by_file(self.deployers_path + '/namespaces/' + self.request['name'] + '.yaml')

    def delete_resource(self):
        if k8s.delete_namespace(self.request['name']):
            path = self.deployers_path + '/namespaces/' + self.request['name'] + '.yaml'
            if os.path.exists(path):
                os.remove(path)

    def delete_namespace_resources(self):
        resources = ['services', 'deployments', 'endpoints', 'secrets']
        k8s.delete_resources_by_label(self.request['name'],
                                      resources, self.deployer_selector + '=' + self.request['deployer'])
