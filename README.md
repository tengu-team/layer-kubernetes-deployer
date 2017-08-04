# Kubernetes-deployer
This subordinate charm serves as a plugin for the Kubernetes cluster and manages deployments. It deploys docker images which are wrapped in the [docker-image](https://github.com/tengu-team/layer-docker-image) layer.

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

## Scaling the application
One pod corresponds to one juju unit. If we want to scale up/down the number of pods we can just add or remove juju units.
```
juju add-unit -n 2 limeds # Deploy 2 more limeds pods
juju remove-unit limeds/2 # Remove a limeds pod
```

## Configuring the application
- `namespace`: Every deployer is limited to one namespace. These namespaces can be shared with other deployers.
- `isolated`: Currently not yet implemented.
- `rolling-updates`: Kubernetes update strategy ([info](https://kubernetes.io/docs/tutorials/kubernetes-basics/update-intro/)).

## Important Notes
- Namespaces which do not have any pods will be removed.



## Authors

This software was created in the [IDLab research group](https://www.ugent.be/ea/idlab) of [Ghent University](https://www.ugent.be) in Belgium. This software is used in [Tengu](https://tengu.io), a project that aims to make experimenting with data frameworks and tools as easy as possible.

 - Sander Borny <sander.borny@ugent.be>
