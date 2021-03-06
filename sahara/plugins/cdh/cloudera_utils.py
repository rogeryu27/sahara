# Copyright (c) 2014 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools

from oslo_log import log as logging
import six

from sahara import context
from sahara.i18n import _
from sahara.plugins.cdh.client import api_client
from sahara.plugins.cdh.client import services
from sahara.plugins.cdh import db_helper
from sahara.plugins import exceptions as ex
from sahara.utils import cluster_progress_ops as cpo
from sahara.utils import poll_utils

LOG = logging.getLogger(__name__)


def cloudera_cmd(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        for cmd in f(*args, **kwargs):
            result = cmd.wait()
            if not result.success:
                if result.children is not None:
                    for c in result.children:
                        if not c.success:
                            raise ex.HadoopProvisionError(c.resultMessage)
                else:
                    raise ex.HadoopProvisionError(result.resultMessage)
    return wrapper


class ClouderaUtils(object):
    CM_DEFAULT_USERNAME = 'admin'
    CM_DEFAULT_PASSWD = 'admin'
    CM_API_VERSION = 6

    HDFS_SERVICE_NAME = 'hdfs01'
    YARN_SERVICE_NAME = 'yarn01'
    OOZIE_SERVICE_NAME = 'oozie01'
    HIVE_SERVICE_NAME = 'hive01'
    HUE_SERVICE_NAME = 'hue01'
    SPARK_SERVICE_NAME = 'spark_on_yarn01'
    ZOOKEEPER_SERVICE_NAME = 'zookeeper01'
    HBASE_SERVICE_NAME = 'hbase01'

    def __init__(self):
        # pu will be defined in derived class.
        self.pu = None

    def get_api_client_by_default_password(self, cluster):
        manager_ip = self.pu.get_manager(cluster).management_ip
        return api_client.ApiResource(manager_ip,
                                      username=self.CM_DEFAULT_USERNAME,
                                      password=self.CM_DEFAULT_PASSWD,
                                      version=self.CM_API_VERSION)

    def get_api_client(self, cluster):
        manager_ip = self.pu.get_manager(cluster).management_ip
        cm_password = db_helper.get_cm_password(cluster)
        return api_client.ApiResource(manager_ip,
                                      username=self.CM_DEFAULT_USERNAME,
                                      password=cm_password,
                                      version=self.CM_API_VERSION)

    def update_cloudera_password(self, cluster):
        api = self.get_api_client_by_default_password(cluster)
        user = api.get_user(self.CM_DEFAULT_USERNAME)
        user.password = db_helper.get_cm_password(cluster)
        api.update_user(user)

    def get_cloudera_cluster(self, cluster):
        api = self.get_api_client(cluster)
        return api.get_cluster(cluster.name)

    @cloudera_cmd
    def start_instances(self, cluster):
        cm_cluster = self.get_cloudera_cluster(cluster)
        yield cm_cluster.start()

    @cpo.event_wrapper(True, step=_("Delete instances"), param=('cluster', 1))
    def delete_instances(self, cluster, instances):
        api = self.get_api_client(cluster)
        cm_cluster = self.get_cloudera_cluster(cluster)
        hosts = api.get_all_hosts(view='full')
        hostsnames_to_deleted = [i.fqdn() for i in instances]
        for host in hosts:
            if host.hostname in hostsnames_to_deleted:
                cm_cluster.remove_host(host.hostId)
                api.delete_host(host.hostId)

    @cpo.event_wrapper(
        True, step=_("Decommission nodes"), param=('cluster', 1))
    def decommission_nodes(self, cluster, process, role_names):
        service = self.get_service_by_role(process, cluster)
        service.decommission(*role_names).wait()
        for role_name in role_names:
            service.delete_role(role_name)

    @cpo.event_wrapper(
        True, step=_("Refresh DataNodes"), param=('cluster', 1))
    def refresh_datanodes(self, cluster):
        self._refresh_nodes(cluster, 'DATANODE', self.HDFS_SERVICE_NAME)

    @cpo.event_wrapper(
        True, step=_("Refresh YARNNodes"), param=('cluster', 1))
    def refresh_yarn_nodes(self, cluster):
        self._refresh_nodes(cluster, 'NODEMANAGER', self.YARN_SERVICE_NAME)

    @cloudera_cmd
    def _refresh_nodes(self, cluster, process, service_name):
        cm_cluster = self.get_cloudera_cluster(cluster)
        service = cm_cluster.get_service(service_name)
        nds = [n.name for n in service.get_roles_by_type(process)]
        for nd in nds:
            for st in service.refresh(nd):
                yield st

    @cpo.event_wrapper(True, step=_("Deploy configs"), param=('cluster', 1))
    @cloudera_cmd
    def deploy_configs(self, cluster):
        cm_cluster = self.get_cloudera_cluster(cluster)
        yield cm_cluster.deploy_client_config()

    def update_configs(self, instances):
        # instances non-empty
        cpo.add_provisioning_step(
            instances[0].cluster_id, _("Update configs"), len(instances))
        with context.ThreadGroup() as tg:
            for instance in instances:
                tg.spawn("update-configs-%s" % instance.instance_name,
                         self._update_configs, instance)

    @cpo.event_wrapper(True)
    @cloudera_cmd
    def _update_configs(self, instance):
        for process in instance.node_group.node_processes:
            process = self.pu.convert_role_showname(process)
            service = self.get_service_by_role(process, instance=instance)
            yield service.deploy_client_config(self.pu.get_role_name(instance,
                                                                     process))

    @cloudera_cmd
    def restart_mgmt_service(self, cluster):
        api = self.get_api_client(cluster)
        cm = api.get_cloudera_manager()
        mgmt_service = cm.get_service()
        yield mgmt_service.restart()

    @cloudera_cmd
    def start_service(self, service):
        yield service.start()

    @cloudera_cmd
    def start_roles(self, service, *role_names):
        for role in service.start_roles(*role_names):
            yield role

    @cpo.event_wrapper(
        True, step=_("Create mgmt service"), param=('cluster', 1))
    def create_mgmt_service(self, cluster):
        api = self.get_api_client(cluster)
        cm = api.get_cloudera_manager()

        setup_info = services.ApiServiceSetupInfo()
        manager = self.pu.get_manager(cluster)
        hostname = manager.fqdn()
        processes = ['SERVICEMONITOR', 'HOSTMONITOR',
                     'EVENTSERVER', 'ALERTPUBLISHER']
        for proc in processes:
            setup_info.add_role_info(self.pu.get_role_name(manager, proc),
                                     proc, hostname)

        cm.create_mgmt_service(setup_info)
        cm.hosts_start_roles([hostname])

    def get_service_by_role(self, role, cluster=None, instance=None):
        cm_cluster = None
        if cluster:
            cm_cluster = self.get_cloudera_cluster(cluster)
        elif instance:
            cm_cluster = self.get_cloudera_cluster(instance.cluster)
        else:
            raise ValueError(_("'cluster' or 'instance' argument missed"))

        if role in ['NAMENODE', 'DATANODE', 'SECONDARYNAMENODE',
                    'HDFS_GATEWAY']:
            return cm_cluster.get_service(self.HDFS_SERVICE_NAME)
        elif role in ['RESOURCEMANAGER', 'NODEMANAGER', 'JOBHISTORY',
                      'YARN_GATEWAY']:
            return cm_cluster.get_service(self.YARN_SERVICE_NAME)
        elif role in ['OOZIE_SERVER']:
            return cm_cluster.get_service(self.OOZIE_SERVICE_NAME)
        elif role in ['HIVESERVER2', 'HIVEMETASTORE', 'WEBHCAT']:
            return cm_cluster.get_service(self.HIVE_SERVICE_NAME)
        elif role in ['HUE_SERVER']:
            return cm_cluster.get_service(self.HUE_SERVICE_NAME)
        elif role in ['SPARK_YARN_HISTORY_SERVER']:
            return cm_cluster.get_service(self.SPARK_SERVICE_NAME)
        elif role in ['SERVER']:
            return cm_cluster.get_service(self.ZOOKEEPER_SERVICE_NAME)
        elif role in ['MASTER', 'REGIONSERVER']:
            return cm_cluster.get_service(self.HBASE_SERVICE_NAME)
        else:
            raise ValueError(
                _("Process %(process)s is not supported by CDH plugin") %
                {'process': role})

    def _agents_connected(self, instances, api):
        hostnames = [i.fqdn() for i in instances]
        hostnames_to_manager = [h.hostname for h in
                                api.get_all_hosts('full')]
        for hostname in hostnames:
            if hostname not in hostnames_to_manager:
                return False
        return True

    @cpo.event_wrapper(True, step=_("Await agents"), param=('cluster', 1))
    def _await_agents(self, cluster, instances, timeout_config):
        api = self.get_api_client(instances[0].cluster)
        poll_utils.plugin_option_poll(
            cluster, self._agents_connected, timeout_config,
            _("Await Cloudera agents"), 5, {
                'instances': instances, 'api': api})

    def configure_instances(self, instances, cluster=None):
        # instances non-empty
        cpo.add_provisioning_step(
            instances[0].cluster_id, _("Configure instances"), len(instances))
        for inst in instances:
            self.configure_instance(inst, cluster)

    def get_roles_list(self, node_processes):
        current = set(node_processes)
        extra_roles = {
            'YARN_GATEWAY': ["YARN_NODEMANAGER"],
            'HDFS_GATEWAY': ['HDFS_NAMENODE', 'HDFS_DATANODE',
                             "HDFS_SECONDARYNAMENODE"]
        }
        for extra_role in six.iterkeys(extra_roles):
            valid_processes = extra_roles[extra_role]
            for valid in valid_processes:
                if valid in current:
                    current.add(extra_role)
                    break
        return list(current)

    def get_role_type(self, process):
        mapper = {
            'YARN_GATEWAY': 'GATEWAY',
            'HDFS_GATEWAY': 'GATEWAY',
        }
        return mapper.get(process, process)

    @cpo.event_wrapper(True)
    def configure_instance(self, instance, cluster=None):
        roles_list = self.get_roles_list(instance.node_group.node_processes)
        for role in roles_list:
            self._add_role(instance, role, cluster)

    def _add_role(self, instance, process, cluster):
        if process in ['CLOUDERA_MANAGER', 'HDFS_JOURNALNODE',
                       'YARN_STANDBYRM']:
            return

        process = self.pu.convert_role_showname(process)
        service = self.get_service_by_role(process, instance=instance)
        role_type = self.get_role_type(process)
        role = service.create_role(self.pu.get_role_name(instance, process),
                                   role_type, instance.fqdn())
        role.update_config(self._get_configs(process, cluster,
                                             instance=instance))

    def get_cloudera_manager_info(self, cluster):
        mng = self.pu.get_manager(cluster)
        info = {
            'Cloudera Manager': {
                'Web UI': 'http://%s:7180' % mng.management_ip,
                'Username': 'admin',
                'Password': db_helper.get_cm_password(cluster)
            }
        }
        return info

    def _get_configs(self, service, cluster=None, instance=None):
        # Defined in derived class.
        return
