# Copyright 2016 rpaas authors. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

import consul
import os

from . import nginx
from misc import host_from_destination

ACL_TEMPLATE = """key "{service_name}/{instance_name}" {{
    policy = "read"
}}

key "{service_name}/{instance_name}/status" {{
    policy = "write"
}}

service "nginx" {{
    policy = "write"
}}
"""


class InstanceAlreadySwappedError(Exception):
    pass


class CertificateNotFoundError(Exception):
    pass


class ConsulManager(object):

    def __init__(self, config):
        host = config.get("CONSUL_HOST")
        port = int(config.get("CONSUL_PORT", "8500"))
        token = config.get("CONSUL_TOKEN")
        self.client = consul.Consul(host=host, port=port, token=token)
        self.config_manager = nginx.ConfigManager(config)
        self.service_name = config.get("RPAAS_SERVICE_NAME", "rpaas")

    def generate_token(self, instance_name):
        rules = ACL_TEMPLATE.format(service_name=self.service_name,
                                    instance_name=instance_name)
        acl_name = "{}/{}/token".format(self.service_name, instance_name)
        return self.client.acl.create(name=acl_name, rules=rules)

    def destroy_token(self, acl_id):
        self.client.acl.destroy(acl_id)

    def destroy_instance(self, instance_name):
        self.client.kv.delete(self._key("{}/".format(instance_name)), recurse=True)

    def write_healthcheck(self, instance_name):
        self.client.kv.put(self._key(instance_name, "healthcheck"), "true")

    def remove_healthcheck(self, instance_name):
        self.client.kv.delete(self._key(instance_name, "healthcheck"))

    def service_healthcheck(self):
        _, instances = self.client.health.service("nginx", tag=self.service_name)
        return instances

    def list_node(self):
        _, nodes = self.client.catalog.nodes()
        return nodes

    def remove_node(self, instance_name, server_name, host_id):
        self.client.kv.delete(self._server_status_key(instance_name, server_name))
        self.client.kv.delete(self._ssl_cert_path(instance_name, "", host_id), recurse=True)
        self.client.agent.force_leave(server_name)

    def node_hostname(self, host):
        for node in self.list_node():
            if node['Address'] == host:
                return node['Node']
        return None

    def node_status(self, instance_name):
        node_status = self.client.kv.get(self._server_status_key(instance_name), recurse=True)
        node_status_list = {}
        if node_status is not None:
            for node in node_status[1]:
                node_server_name = node['Key'].split('/')[-1]
                node_status_list[node_server_name] = node['Value']
        return node_status_list

    def write_location(self, instance_name, path, destination=None, content=None, router_mode=False,
                       bind_mode=False, https_only=False):
        if content:
            content = content.strip()
        else:
            upstream, _ = host_from_destination(destination)
            upstream_server = upstream
            if bind_mode:
                upstream = "rpaas_default_upstream"
            content = self.config_manager.generate_host_config(path, destination, upstream, router_mode, https_only)
            if router_mode:
                upstream_server = None
            self.add_server_upstream(instance_name, upstream, upstream_server)
        self.client.kv.put(self._location_key(instance_name, path), content)

    def remove_location(self, instance_name, path):
        self.client.kv.delete(self._location_key(instance_name, path))

    def write_block(self, instance_name, block_name, content):
        content = self._set_header_footer(content, block_name)
        self.client.kv.put(self._block_key(instance_name, block_name), content)

    def remove_block(self, instance_name, block_name):
        self.write_block(instance_name, block_name, None)

    def list_blocks(self, instance_name, block_name=None):
        blocks = self.client.kv.get(self._block_key(instance_name, block_name),
                                    recurse=True)
        block_list = []
        if blocks[1]:
            for block in blocks[1]:
                block_name = block['Key'].split('/')[-2]
                block_value = self._set_header_footer(block['Value'], block_name, True)
                if not block_value:
                    continue
                block_list.append({'block_name': block_name, 'content': block_value})
        return block_list

    def _set_header_footer(self, content, block_name, remove=False):
        begin_block = "## Begin custom RpaaS {} block ##\n".format(block_name)
        end_block = "## End custom RpaaS {} block ##".format(block_name)
        if remove:
            content = content.replace(begin_block, "")
            content = content.replace(end_block, "")
            return content.strip()
        if content:
            content = begin_block + content.strip() + '\n' + end_block
        else:
            content = begin_block + end_block
        return content

    def write_lua(self, instance_name, lua_module_name, lua_module_type, content):
        content_block = self._lua_module_escope(lua_module_name, content)
        key = self._lua_key(instance_name, lua_module_name, lua_module_type)
        return self.client.kv.put(key, content_block)

    def _lua_module_escope(self, lua_module_name, content=""):
        begin_escope = "-- Begin custom RpaaS {} lua module --".format(lua_module_name)
        end_escope = "-- End custom RpaaS {} lua module --".format(lua_module_name)
        content_stripped = ""
        if content:
            content_stripped = content.strip()
        escope = "{0}\n{1}\n{2}".format(begin_escope, content_stripped, end_escope)
        return escope

    def list_lua_modules(self, instance_name):
        modules = self.client.kv.get(self._lua_key(instance_name), recurse=True)
        module_list = []
        if modules[1]:
            for module in modules[1]:
                module_name = module['Key'].split('/')[-2]
                module_value = module['Value']
                module_list.append({'module_name': module_name, 'content': module_value})
        return module_list

    def remove_lua(self, instance_name, lua_module_name, lua_module_type):
        self.write_lua(instance_name, lua_module_name, lua_module_type, None)

    def add_server_upstream(self, instance_name, upstream_name, server):
        if not server:
            return
        servers = self.list_upstream(instance_name, upstream_name)
        if isinstance(server, list):
            for idx, _ in enumerate(server):
                server[idx] = ":".join(map(str, filter(None, host_from_destination(server[idx]))))
            servers |= set(server)
        else:
            server = ":".join(map(str, filter(None, host_from_destination(server))))
            servers.add(server)
        self._save_upstream(instance_name, upstream_name, servers)

    def remove_server_upstream(self, instance_name, upstream_name, server):
        servers = self.list_upstream(instance_name, upstream_name)
        if isinstance(server, list):
            for idx, _ in enumerate(server):
                server[idx] = ":".join(map(str, filter(None, host_from_destination(server[idx]))))
            servers -= set(server)
        else:
            server = ":".join(map(str, filter(None, host_from_destination(server))))
            if server in servers:
                servers.remove(server)
        if len(servers) < 1:
            self._remove_upstream(instance_name, upstream_name)
        else:
            self._save_upstream(instance_name, upstream_name, servers)

    def _remove_upstream(self, instance_name, upstream_name):
        content = self._set_header_footer(None, "upstream")
        self.client.kv.put(self._upstream_key(instance_name, upstream_name), content)

    def list_upstream(self, instance_name, upstream_name):
        servers = self.client.kv.get(self._upstream_key(instance_name, upstream_name))[1]
        if servers:
            servers = self._set_header_footer(servers["Value"], "upstream", True)
            if servers == "":
                return set()
            return set(servers.split(","))
        return set()

    def _save_upstream(self, instance_name, upstream_name, servers):
        content = self._set_header_footer(",".join(servers), "upstream")
        self.client.kv.put(self._upstream_key(instance_name, upstream_name), content)

    def swap_instances(self, src_instance, dst_instance):
        if not self.check_swap_state(src_instance, dst_instance):
            raise InstanceAlreadySwappedError()
        src_instance_value = self.client.kv.get(self._key(src_instance, "swap"))[1]
        if not src_instance_value:
            self.client.kv.put(self._key(src_instance, "swap"), dst_instance)
            self.client.kv.put(self._key(dst_instance, "swap"), src_instance)
            return
        if src_instance_value['Value'] == dst_instance:
            self.client.kv.delete(self._key(src_instance, "swap"))
            self.client.kv.delete(self._key(dst_instance, "swap"))
            return
        self.client.kv.put(self._key(src_instance, "swap"), dst_instance)
        self.client.kv.put(self._key(dst_instance, "swap"), src_instance)

    def check_swap_state(self, src_instance, dst_instance):
        src_instance_status = self.client.kv.get(self._key(src_instance, "swap"))[1]
        dst_instance_status = self.client.kv.get(self._key(dst_instance, "swap"))[1]
        if not src_instance_status and not dst_instance_status:
            return True
        if not src_instance_status or not dst_instance_status:
            return False
        if sorted([src_instance_status['Value'], dst_instance_status['Value']]) != sorted([src_instance, dst_instance]):
            return False
        return True

    def find_acl_network(self, instance_name, src=None):
        src = self._normalize_acl_src(src)
        acls = self.client.kv.get(self._acl_key(instance_name, src), recurse=True)[1]
        if not acls:
            return []
        acls_list = []
        for acl in acls:
            src = acl['Key'].split('/')[-1]
            acls_list.append({"source": self._normalize_acl_src(src),
                              "destination": acl["Value"].split(",")})
        return acls_list

    def store_acl_network(self, instance_name, src, dst):
        acls = self.find_acl_network(instance_name, src)
        if acls:
            acls = set(acls[0]['destination'])
            acls |= set([dst])
        else:
            acls.append(dst)
        src = self._normalize_acl_src(src)
        self.client.kv.put(self._acl_key(instance_name, src), ",".join(acls))

    def remove_acl_network(self, instance_name, src):
        src = self._normalize_acl_src(src)
        self.client.kv.delete(self._acl_key(instance_name, src))

    def _normalize_acl_src(self, src):
        if not src:
            return
        if "_" in src:
            return src.replace("_", "/")
        return src.replace("/", "_")

    def get_certificate(self, instance_name, host_id=None):
        cert = self.client.kv.get(self._ssl_cert_path(instance_name, "cert", host_id))[1]
        key = self.client.kv.get(self._ssl_cert_path(instance_name, "key", host_id))[1]
        if not cert or not key:
            raise CertificateNotFoundError()
        return cert["Value"], key["Value"]

    def set_certificate(self, instance_name, cert_data, key_data, host_id=None):
        self.client.kv.put(self._ssl_cert_path(instance_name, "cert", host_id),
                           cert_data.replace("\r\n", "\n"))
        self.client.kv.put(self._ssl_cert_path(instance_name, "key", host_id),
                           key_data.replace("\r\n", "\n"))

    def delete_certificate(self, instance_name):
        self.client.kv.delete(self._ssl_cert_path(instance_name, "cert"))
        self.client.kv.delete(self._ssl_cert_path(instance_name, "key"))

    def _ssl_cert_path(self, instance_name, key_type, host_id=None):
        if host_id:
            return os.path.join(self._key(instance_name, "ssl/{}".format(host_id)), key_type)
        return os.path.join(self._key(instance_name, "ssl"), key_type)

    def _location_key(self, instance_name, path):
        location_key = "ROOT"
        if path != "/":
            location_key = path.replace("/", "___")
        return self._key(instance_name, "locations/" + location_key)

    def _block_key(self, instance_name, block_name=None):
        block_key = "ROOT"
        if block_name:
            block_path_key = self._key(instance_name,
                                       "blocks/%s/%s" % (block_name,
                                                         block_key))
        else:
            block_path_key = self._key(instance_name, "blocks")
        return block_path_key

    def _server_status_key(self, instance_name, server_name=None):
        if server_name:
            return self._key(instance_name, "status/%s" % server_name)
        return self._key(instance_name, "status")

    def _lua_key(self, instance_name, lua_module_name="", lua_module_type=""):
        base_key = "lua_module"
        if lua_module_name and lua_module_type:
            base_key = "lua_module/{0}/{1}".format(lua_module_type, lua_module_name)
        return self._key(instance_name, base_key)

    def _upstream_key(self, instance_name, upstream_name):
        base_key = "upstream/{}".format(upstream_name)
        return self._key(instance_name, base_key)

    def _acl_key(self, instance_name, src=None):
        base_key = "acl"
        if src:
            base_key = "acl/{}".format(src)
        return self._key(instance_name, base_key)

    def _key(self, instance_name, suffix=None):
        key = "{}/{}".format(self.service_name, instance_name)
        if suffix:
            key += "/" + suffix
        return key
