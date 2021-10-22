# ccm clusters
import os
import shutil
import time
import subprocess
import signal
import yaml
import uuid
import datetime
import getpass
import socket

from fabric import Connection
from invoke.exceptions import UnexpectedExit, Failure
from six import print_
from distutils.version import LooseVersion

from ccmlib import common
from ccmlib.cluster import Cluster
from ccmlib.scylla_node import ScyllaNode
from ccmlib.node import NodeError
from ccmlib import scylla_repository

SNITCH = 'org.apache.cassandra.locator.GossipingPropertyFileSnitch'

KUBECTL_BIN = "kubectl"
SCYLLA_MANAGER_NAMESPACE = "scylla-manager"


class LocalCLI:
    def __init__(self):
        self.hostname = socket.gethostname()
        self.user = getpass.getuser()
        self._connection = None

    @property
    def connection(self):
        if self._connection is None:
            self._connection = self._create_connection()
        return self._connection

    def __str__(self):
        return '{} [{}@{}]'.format(self.__class__.__name__, self.user, self.hostname)

    def _create_connection(self):
        return Connection(host=self.hostname, user=self.user)

    def run(self, cmd: str, timeout=None, ignore_status=False, new_session=False):
        command_kwargs = {
            "command": cmd,
            "encoding": 'utf-8',
            "timeout": timeout,
            "warn": ignore_status,
            "hide": True,
            "env": os.environ,
            "replace_env": True,
            "in_stream": False,
        }
        if new_session:
            with self._create_connection() as connection:
                result = connection.local(**command_kwargs)
        else:
            result = self.connection.local(**command_kwargs)
        result.exit_status = result.exited
        return result


class KubeCTL:
    def __init__(self, kubeconfig):
        self.kubeconfig = kubeconfig
        self.local_cli = LocalCLI()

    def kubectl(self, *command, namespace=None, timeout=300, ignore_status=False):
        cmd = [KUBECTL_BIN, f"--kubeconfig={self.kubeconfig}"]
        if namespace:
            cmd.append(f"--namespace={namespace}")
        cmd.extend(command)
        return self.local_cli.run(cmd, timeout=timeout, ignore_status=ignore_status, verbose=verbose)


class ScyllaClusterOnK8S(Cluster):
    def __init__(self,
                 name,
                 datacenter='kinddc',
                 kubeconfig='',
                 partitioner=None,
                 scylla_version=None,
                 **kwargs):
        self.scylla_reloc = False
        self.scylla_mode = None
        self.started = False
        self.force_wait_for_cluster_start = True

        self.name = name
        self.partitioner = partitioner
        self.scylla_version, self.__version = (scylla_version, ) * 2

        self.kubectl = KubeCTL(kubeconfig=kubeconfig)
        self._scylla_manager = ScyllaManagerOnK8S(self)

        # TODO: create ScyllaCluster CRD and set 0 'members' there

    def set_configuration_options(self, values=None, batch_commitlog=None):
        # TODO: create scylla_config 'configMap' K8S object with the "scylla.yaml" file
        #       and populate it with provided options.
        return self

    def populate(self, nodes, debug=False, tokens=None, use_vnodes=False, ipprefix=None, ipformat=None):
        # TODO: update "ScyllaCluster" CRD with the "members={nodes}" value which will cause Scylla nodes creation
        return self

    def new_node(self, i, auto_bootstrap=False,
                 debug=False, initial_token=None,
                 add_node=True, is_seed=True, data_center=None):
        # TODO: increase 'members' count in the ScyllaCluster CRD
        raise NotImplementedError()

    def create_node(self, name, auto_bootstrap, thrift_interface,
                    storage_interface, jmx_port, remote_debug_port,
                    initial_token, save=True, binary_interface=None):
        NotImplementedError()

    def add(self, node, is_seed, data_center=None):
        raise NotImplementedError()

    def get_node_ip(self, nodeid):
        # TODO: get K8S's Service object IP that is dedicated for a Scylla pod hosting a Scylla node
        #       It will be available from host machine setting following ip route:
        #       $ ip ro add $K8S_SERVIES_NETWORK via $SERVICE_GATEWAY
        #       where $K8S_SERVIES_NETWORK is like '10.0.0.0/16' and configured in 'kind' tool
        #       and $SERVICE_GATEWAY is kind-control-plane's IP address.
        raise NotImplementedError()

    def start_nodes(self, nodes=None, no_wait=True, verbose=False, wait_for_binary_proto=None,
              wait_other_notice=None, jvm_args=None, profile_options=None,
              quiet_start=False):
        # NOTE: not needed on K8S
        raise NotImplementedError()

    def start(self, no_wait=True, verbose=False, wait_for_binary_proto=None,
              wait_other_notice=None, jvm_args=None, profile_options=None,
              quiet_start=False):
        # NOTE: not needed on K8S
        raise NotImplementedError()

    def stop_nodes(self, nodes=None, wait=True, gently=True, wait_other_notice=False, other_nodes=None, wait_seconds=None):
        # NOTE: not needed on K8S
        raise NotImplementedError()

    def stop(self, wait=True, gently=True, wait_other_notice=False, other_nodes=None, wait_seconds=None):
        # NOTE: not needed on K8S
        raise NotImplementedError()

    def enable_internode_ssl(self, node_ssl_path, internode_encryption='all'):
        raise NotImplementedError()

    def sctool(self, cmd):
        return self._scylla_manager.sctool(cmd)

    def start_scylla_manager(self):
        # NOTE: K8S has one Scylla manager for all the Scylla clusters. So, this method is not relevant. Skip it.
        return

    def stop_scylla_manager(self, gently=True):
        # NOTE: K8S has one Scylla manager for all the Scylla clusters. So, this method is not relevant. Skip it.
        return

    def upgrade_cluster(self, upgrade_version):
        NotImplementedError(
            "Upgrade on K8S can be implemented using docker images and not rellocatable packages "
            "which are used by tests. So, after implementation of this method need to update upgrade tests too.")


# TODO: implement whole class
class ScyllaNodeOnK8S:
    def __init__(self):
        pass


class ScyllaManagerOnK8S:
    def __init__(self, scylla_cluster):
        self.scylla_cluster = scylla_cluster
        self.kubectl = scylla_cluster.kubectl

    @property
    def version(self):
        image = self.kubectl(
            "get pods --no-headers "
            "-l app.kubernetes.io/name=scylla-manager "
            "-o=custom-columns=:.spec.containers[0].image",
            namespace=SCYLLA_MANAGER_NAMESPACE).stdout
        return LooseVersion(image.split(":")[-1])

    @property
    def is_agent_available(self):
        # NOTE: all Scylla K8S pods have manager agent running
        return True

    def start(self):
        # NOTE: K8S has one Scylla manager for all the Scylla clusters. So, this method is not relevant. Skip it.
        return

    def stop(self, gently):
        # NOTE: K8S has one Scylla manager for all the Scylla clusters. So, this method is not relevant. Skip it.
        return

    def sctool(self, cmd, ignore_exit_status=False):
        result = self.kubectl(f"sctool {" ".join(cmd)}", ignore_status=ignore_exit_status)
        return result.stdout, result.stderr
