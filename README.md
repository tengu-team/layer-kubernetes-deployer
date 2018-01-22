
# Kubernetes-deployer
This subordinate charm serves as a plugin for the Kubernetes cluster and manages a namespace. It support the following operations:
 - Creating resources using the [kubernetes-deployer](https://github.com/tengu-team/interface-kubernetes-deployer) interface.
 - Adding networkpolicies on a namespace level.

## Using the Charm
The charm is subordinate to a kubernetes master.
```
juju deploy ./kubernetes-deployer deployer
juju add-relation deployer kubernetes-master
```
## Configuring the application
- `namespace`: Every deployer is limited to one namespace. **These namespaces should be unique per deployer charm!**
- `isolated`: Requires a Kubernetes cluster with network policy support such as the [canal](https://jujucharms.com/canonical-kubernetes-canal/) bundle. If true all pods within the namespace are isolated.

## Important Notes
- Namespaces which do not have any resources will be removed.



## Authors

This software was created in the [IDLab research group](https://www.ugent.be/ea/idlab) of [Ghent University](https://www.ugent.be) in Belgium. This software is used in [Tengu](https://tengu.io), a project that aims to make experimenting with data frameworks and tools as easy as possible.

 - Sander Borny <sander.borny@ugent.be>
 - Icon made by [Roundicons](http://www.freepik.com) from [www.flaticon.com](www.flaticon.com) licensed as [Creative Commons BY 3.0](http://creativecommons.org/licenses/by/3.0/)
