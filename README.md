# Layer-kubernetes-deployer
This layer serves as an extra layer for the [kubernetes-master](https://jujucharms.com/u/containers/kubernetes-master/) charm. It deploys docker images which are wrapped in the [docker-image](https://github.com/tengu-team/layer-docker-image) layer.

## Building the layer
Using this layer requires some modifications to the [kubernetes-master](https://jujucharms.com/u/containers/kubernetes-master/) layer. First download the layer from the [Github repository](https://github.com/kubernetes/kubernetes/tree/master/cluster/juju/layers/kubernetes-master). Then modify the `layer.yaml` to include the kubernetes-deployer layer. Rebuild the charm and setup your custom Kubernetes cluster.

## Using the layer
As an example we use the [limeds](https://github.com/tengu-team/layer-limeds) charm. Which uses ports 8080 and 8443. For now we just want port 8080 to be reachable outside the cluster. Important note, the ports config assumes a yaml formatted dictionary.
```
juju deploy limeds
juju config limeds 'ports=8080: "exposed"
                          8443: ""'
juju add-relation limeds kubernetes-master
```

## Scaling the application
One pod corresponds to one juju unit. If we want to scale up/down the number of pods we can just add or remove juju units.
```
juju add-unit -n 2 limeds # Deploy 2 more limeds pods
juju remove-unit limeds/2 # Remove a limeds pod
```

## Private Images
Using private images requires the following config values to be set (in the docker-image based charm):
- username
- password
- docker-registry


## Authors

This software was created in the [IDLab research group](https://www.ugent.be/ea/idlab) 
of [Ghent University](https://www.ugent.be) in Belgium. This software is used in 
[Tengu](https://tengu.io), a project that aims to make experimenting with data 
frameworks and tools as easy as possible.

 - Sander Borny <sander.borny@ugent.be>