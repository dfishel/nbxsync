import logging

from django.contrib.contenttypes.models import ContentType

from nbxsync.choices.zabbixstatus import ZabbixHostStatus
from nbxsync.models import ZabbixServerAssignment
from nbxsync.settings import get_plugin_settings
from nbxsync.utils import get_assigned_zabbixobjects
from nbxsync.utils.sync import HostGroupSync, HostSync, ProxyGroupSync, ProxySync
from nbxsync.utils.sync.safe_delete import safe_delete
from nbxsync.utils.sync.safe_sync import safe_sync
from nbxsync.utils.trigger_dependency_sync import sync_device_trigger_dependencies

logger = logging.getLogger(__name__)

__all__ = ('SyncHostJob',)


class SyncHostJob:
    def __init__(self, **kwargs):
        self.instance = kwargs.get('instance')  # This is the Device or VirtualMachine object

    def run(self):
        object_ct = ContentType.objects.get_for_model(self.instance)

        zabbixserver_assignments = ZabbixServerAssignment.objects.filter(assigned_object_type=object_ct, assigned_object_id=self.instance.pk)

        status = self.instance.status
        object_type = self.instance._meta.model_name  # "device" or "virtualmachine"
        pluginsettings = get_plugin_settings()
        status_mapping = getattr(pluginsettings.statusmapping, object_type, {})
        zabbix_status = status_mapping.get(status)

        for assignment in zabbixserver_assignments:
            if not assignment.sync_enabled or not assignment.zabbixserver.sync_enabled:
                continue

            if zabbix_status == ZabbixHostStatus.DELETED:
                self.delete_host(assignment)
            else:
                self.sync_host(assignment)

            if object_type == 'device' and zabbix_status != ZabbixHostStatus.DELETED and pluginsettings.trigger_dependencies.enabled:
                try:
                    sync_device_trigger_dependencies(self.instance)
                except Exception:
                    logger.exception('Trigger dependency sync failed for %s; continuing.', self.instance)

    def delete_host(self, assignment):
        safe_delete(HostSync, assignment)

    def sync_host(self, assignment):
        try:
            all_objects = get_assigned_zabbixobjects(self.instance, zabbixserver=assignment.zabbixserver)
            # Add the assigned_objects attribute, so we dont have to do this expensive calculation again later on :)
            assignment.assigned_objects = all_objects

            # Create all hostgroups
            for hostgroup in all_objects['hostgroups']:
                safe_sync(HostGroupSync, hostgroup)

            # Sync ProxyGroups and proxies (in that order!)
            # If the ZabbixServer Assignment has a Proxy, sync it
            if assignment.zabbixproxy:
                # If the ZabbixProxy is assigned to a ProxyGroup, sync the group first.
                if assignment.zabbixproxy.proxygroup:
                    safe_sync(ProxyGroupSync, assignment.zabbixproxy.proxygroup)
                safe_sync(ProxySync, assignment.zabbixproxy)

            # If the ZabbixServer Assignment has a ProxyGroup, sync it
            if assignment.zabbixproxygroup:
                safe_sync(ProxyGroupSync, assignment.zabbixproxygroup)

            # Sync the actual Host, including its host interfaces.
            # Zabbix 8.0 removed the hostinterface.create/update/delete methods, so interfaces are
            # sent along with host.create / host.update (this works for older Zabbix versions as well).
            try:
                safe_sync(HostSync, assignment, extra_args={'all_objects': all_objects})
            except Exception:
                # A newly created host doesn't get its templates linked on creation; the second pass below links them
                # and surfaces any error.
                pass

            safe_sync(HostSync, assignment, extra_args={'all_objects': all_objects})

        except Exception as e:
            raise RuntimeError(f'Unexpected error: {e}')
