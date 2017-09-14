# Kubernetes-deployer
This subordinate charm serves as a plugin for the Kubernetes cluster and manages a namespace. It support the following operations:
 - Creating deployments, services and secrets for charms using the [docker-image](https://github.com/tengu-team/layer-docker-image) layer.
 - Creating services for external resources.
 - Adding networkpolicies on a namespace level.

## Using the Charm
The charm is subordinate to a kubernetes master.
```
juju deploy ./kubernetes-deployer deployer
juju add-relation deployer kubernetes-master
```
As an example we use the [limeds](https://github.com/tengu-team/layer-limeds) charm. Which uses ports 8080 and 8443. For now we just want port 8080 to be reachable outside the cluster. Important note, the ports config assumes a yaml formatted dictionary.
```
juju deploy limeds
juju config limeds 'ports=8080: "exposed"
                          8443: ""'
juju add-relation limeds deployer
```

## Scaling the number of pods
One pod corresponds to one juju unit. If we want to scale up/down the number of pods we can just add or remove juju units.
```
juju add-unit -n 2 limeds # Deploy 2 more limeds pods
juju remove-unit limeds/2 # Remove a limeds pod
```

## Configuring the application
- `namespace`: Every deployer is limited to one namespace. **These namespaces should be unique per deployer charm!**
- `isolated`: Requires a Kubernetes cluster with network policy support. If true all pods within the namespace are isolated.
- `rolling-updates`: Kubernetes update strategy ([info](https://kubernetes.io/docs/tutorials/kubernetes-basics/update-intro/)).

## Important Notes
- Namespaces which do not have any resources will be removed.



## Authors

This software was created in the [IDLab research group](https://www.ugent.be/ea/idlab) of [Ghent University](https://www.ugent.be) in Belgium. This software is used in [Tengu](https://tengu.io), a project that aims to make experimenting with data frameworks and tools as easy as possible.

 - Sander Borny <sander.borny@ugent.be>
 - Icon made by [Roundicons](http://www.freepik.com) from [www.flaticon.com](www.flaticon.com) licensed as [Creative Commons BY 3.0](http://creativecommons.org/licenses/by/3.0/)

