import os
from . import k8shelpers as k8s
from charmhelpers.core.templating import render
from charmhelpers.core import unitdata
from charmhelpers.core.hookenv import log, config

config = config()


class ResourceFactory(object):
    @staticmethod
    def create_resource(resource_type, request):
        if resource_type == 'deployment':
            return Deployment(request)
        elif resource_type == 'namespace':
            return Namespace(request)
        elif resource_type == 'secret':
            return Secret(request)
        elif resource_type == 'service':
            return Service(request)
        elif resource_type == 'headless-service':
            return HeadlessService(request)
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


class HeadlessService(Resource):
    """request = {
        'name': name of the service,
        'namespace': namespace of the service,
        'port': port for the service,
        'ips': addresses for the service
    }    
    """
    def write_resource_file(self):
        render(source='headless-service.tmpl',
               target=self.deployer_path + '/headless-services/' + self.request['name'] + '.yaml',
               context={
                   'name': self.name(),
                   'namespace': self.request['namespace'],
                   'juju_selector': self.juju_app_selector,
                   'uname': self.request['name'],
                   'deployer_selector': self.deployer_selector,
                   'deployer': self.deployer_name,
                   'port': self.request['port'],
                   'ips': self.request['ips']
               })

    def create_resource(self):
        k8s.create_resources(self.deployer_path)

    def name(self):
        return self.request['name'] + '-headless-service'

    def delete_resource(self):
        k8s.delete_resource_by_name(self.request['namespace'],
                                    'service',
                                    self.name())


class Service(Resource):
    """request = {
        'name': name of the service,
        'ports': port for the service,
        'namespace': namespace of the service
    }    
    """
    def write_resource_file(self):
        service_context = {
            'name': self.name(),
            'uname': self.request['name'],
            'ports': self.request['ports'],
            'namespace': self.request['namespace'],
            'juju_selector': self.juju_app_selector,
            'deployer_selector': self.deployer_selector,
            'deployer': self.deployer_name
        }
        render(source='service.tmpl',
               target=self.deployer_path + '/services/' + self.request['name'] + '.yaml',
               context=service_context)

    def create_resource(self):
        k8s.create_resources(self.deployer_path)

    def name(self):
        return self.request['name'] + '-service'

    def delete_resource(self):
        k8s.delete_resource_by_name(self.request['namespace'],
                                    'service',
                                    self.name())
        # os.remove(self.deployer_path + '/secrets/' + self.request['app'] + '.yaml')


class Deployment(Resource):
    """request = {
        'name': name of the deployment,
        'replicas': nr of pods,
        'image': docker image,
        'namespace': namespace for deployment,
        'rolling': rolling updates (bool),
        'env_vars': env vars for pods,
        'env_order': order of env vars,
        'secret': secret to use
    }
    """
    def write_resource_file(self):
        log('Writing resource file for deployment')
        deployment_context = {
                   'name': self.name(),
                   'uname': self.request['name'],
                   'replicas': self.request['replicas'],
                   'image': self.request.get('image'),
                   'namespace': config.get('namespace').rstrip(),
                   'rolling': config.get('rolling-updates'),
                   'env_vars': self.request['env_vars'],
                   'juju_selector': self.juju_app_selector,
                   'deployer_selector': self.deployer_selector,
                   'deployer': self.deployer_name
               }
        if 'env_order' in self.request:
            deployment_context['env_order'] = self.request['env_order']
        if 'secret' in self.request:
            deployment_context['imagesecret'] = self.request['secret']
        render(source='deployment.tmpl',
               target=self.deployer_path + '/deployments/' + self.request['name'] + '.yaml',
               context=deployment_context)

    def name(self):
        return self.request['name'] + '-deployment'

    def delete_resource(self):
        k8s.delete_resource_by_name(config.get('namespace').rstrip(),
                                    'deployment',
                                    self.request['app'] + '-deployment')
        # os.remove(self.deployer_path + '/deployments/' + self.request['app'] + '.yaml')

    def create_resource(self):
        k8s.create_resources(self.deployer_path)


class Secret(Resource):
    """request = {
        'username': docker username,
        'password': docker password,
        'docker-registry': docker registry (default https://index.docker.io/v1/),
        'deployer': name of the deployer,
        'app': name of the app requiring the secret,
        'namespace': namespace of the secret
        
        Secret requests will be saved into the kv store in order to monitor changes. Using files to check
        if the secret updates does not work since every iteration a new file name is given and base64 encoded.
    }    
    """
    def write_resource_file(self):
        log('Docker secret does not require resource file.')
        raise NotImplementedError()

    def create_resource(self):
        # Check if resource already exists
        if self.secret_exists():
            # Check if new secret is needed
            if unitdata.kv().get(self.request['app']) == self.request:
                return
            else:
                self.delete_resource()
        k8s.create_secret(self.request['namespace'],
                          self.request['app'],
                          self.request['username'],
                          self.request['password'],
                          self.juju_app_selector + '=' + self.request['app'],
                          self.deployer_selector + '=' + self.request['deployer'],
                          self.request['docker-registry'])
        unitdata.kv().set(self.request['app'], self.request)

    def secret_exists(self):
        return k8s.secret_exists(self.request['app'], self.request['namespace'])

    def delete_resource(self):
        k8s.delete_secret(self.request['app'], self.request['namespace'])

    def name(self):
        return self.request['app']


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
