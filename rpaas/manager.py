# Copyright 2015 rpaas authors. All rights reserved.
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file.

# coding: utf-8

import hm.managers.cloudstack  # NOQA
import hm.lb_managers.networkapi_cloudstack  # NOQA
from hm.model.load_balancer import LoadBalancer

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
import os
from base64 import b64encode
import socket

from rpaas import storage, tasks, nginx


PENDING = 'pending'
FAILURE = 'failure'


class Manager(object):
    def __init__(self, config=None):
        self.config = config
        self.storage = storage.MongoDBStorage(config)
        self.nginx_manager = nginx.NginxDAV(config)

    def new_instance(self, name, team=None):
        used, quota = self.storage.find_team_quota(team)
        if len(used) >= quota:
            raise QuotaExceededError(len(used), quota)
        if not self.storage.increment_quota(team, used, name):
            raise Exception("concurrent operations updating team quota")
        lb = LoadBalancer.find(name)
        if lb is not None:
            raise storage.DuplicateError(name)
        self.storage.store_task(name)
        task = tasks.NewInstanceTask().delay(self.config, name)
        self.storage.update_task(name, task.task_id)

    def remove_instance(self, name):
        self.storage.decrement_quota(name)
        self.storage.remove_task(name)
        self.storage.remove_binding(name)
        tasks.RemoveInstanceTask().delay(self.config, name)

    def bind(self, name, app_host):
        self._ensure_ready(name)
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        binding_data = self.storage.find_binding(name)
        if binding_data:
            binded_host = binding_data.get('app_host')
            if binded_host == app_host:
                # Nothing to do, already binded
                return
            if binded_host is not None:
                raise BindError('This service can only be binded to one application.')
        for host in lb.hosts:
            self.nginx_manager.update_binding(host.dns_name, '/', app_host)
        self.storage.store_binding(name, app_host)

    def unbind(self, name, app_host):
        self._ensure_ready(name)
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        binding_data = self.storage.find_binding(name)
        if not binding_data:
            return
        self.storage.remove_root_binding(name)
        for host in lb.hosts:
            self.nginx_manager.delete_binding(host.dns_name, '/')

    def info(self, name):
        addr = self._get_address(name)
        routes_data = []
        binding_data = self.storage.find_binding(name)
        if binding_data:
            paths = binding_data.get('paths') or []
            for path_data in paths:
                routes_data.append("path = {}".format(path_data['path']))
                dst = path_data.get('destination')
                content = path_data.get('content')
                if dst:
                    routes_data.append("destination = {}".format(dst))
                if content:
                    routes_data.append("content = {}".format(content))
        lb = LoadBalancer.find(name)
        host_count = 0
        if lb:
            host_count = len(lb.hosts)
        return [
            {
                "label": "Address",
                "value": addr,
            },
            {
                "label": "Instances",
                "value": str(host_count),
            },
            {
                "label": "Routes",
                "value": "\n".join(routes_data),
            },
        ]

    def status(self, name):
        return self._get_address(name)

    def update_certificate(self, name, cert, key):
        self._ensure_ready(name)
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        self.storage.update_binding_certificate(name, cert, key)
        for host in lb.hosts:
            self.nginx_manager.update_certificate(host.dns_name, cert, key)

    def _get_address(self, name):
        task = self.storage.find_task(name)
        if task:
            result = tasks.NewInstanceTask().AsyncResult(task['task_id'])
            if result.status in ['FAILURE', 'REVOKED']:
                return FAILURE
            return PENDING
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        return lb.address

    def scale_instance(self, name, quantity):
        self._ensure_ready(name)
        if quantity <= 0:
            raise ScaleError("Can't have 0 instances")
        self.storage.store_task(name)
        task = tasks.ScaleInstanceTask().delay(self.config, name, quantity)
        self.storage.update_task(name, task.task_id)

    def add_route(self, name, path, destination, content):
        self._ensure_ready(name)
        path = path.strip()
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        self.storage.replace_binding_path(name, path, destination, content)
        for host in lb.hosts:
            self.nginx_manager.update_binding(host.dns_name, path, destination, content)

    def delete_route(self, name, path):
        self._ensure_ready(name)
        path = path.strip()
        if path == '/':
            raise RouteError("You cannot remove a route for / location, unbind the app.")
        lb = LoadBalancer.find(name)
        if lb is None:
            raise storage.InstanceNotFoundError()
        self.storage.delete_binding_path(name, path)
        for host in lb.hosts:
            self.nginx_manager.delete_binding(host.dns_name, path)

    def list_routes(self, name):
        return self.storage.find_binding(name)

    def _ensure_ready(self, name):
        task = self.storage.find_task(name)
        if task:
            raise NotReadyError("Async task still running")

    def _generate_key(self):
        # Generate our key
        key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        # Write our key to disk for safe keeping
        with open("/etc/nginx/sites-enabled/dav/ssl/key.pem", "wb") as f:
            passphrase = b64encode(os.urandom(16))
            f.write(key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.BestAvailableEncryption(passphrase),
            ))
        return key

    def _generate_csr(self, key, domainname):
        # Generate a CSR
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
            # Provide various details about who we are.
            x509.NameAttribute(NameOID.COUNTRY_NAME, u"BR"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, u"RJ"),
            x509.NameAttribute(NameOID.LOCALITY_NAME, u"Rio de Janeiro"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, u"globo.com"),
            x509.NameAttribute(NameOID.COMMON_NAME, domainname),
        ])).add_extension(
            x509.SubjectAlternativeName([
                # Sites we want this certificate for.
                x509.DNSName(domainname),
            ]),
            critical=False,
        # Sign the CSR with our private key.
        ).sign(key, hashes.SHA256(), default_backend())
        # Write the CSR out to disk.
        with open("/etc/nginx/sites-enabled/dav/ssl/csr.pem", "wb") as f:
            f.write(csr.public_bytes(serialization.Encoding.PEM))
        return csr

    def _check_dns(self, domain):
        ''' Check if the DNS name is registered for the rpaas VIP
        @param domain Domain name
        @param vip rpaas ip
        '''
        address = self._get_address(name)
        if address == FAILURE or address == PENDING:
            return False

        answer = socket.getaddrinfo(domain, 0,0,0,0)
        if address not in [ip[4][0] for ip in answer]:
            return False

        return True

    def activate_ssl(self, name, domain):
        # Check if DNS is registered for rpaas ip
        if not self._check_dns(domain):
            return 'rpaas IP is not registered for this DNS name'

        # Key and CSR generated to request a certificate
        key = self._generate_key()
        csr = self._generate_csr(key, domain)



class BindError(Exception):
    pass


class NotReadyError(Exception):
    pass


class ScaleError(Exception):
    pass


class RouteError(Exception):
    pass


class QuotaExceededError(Exception):
    def __init__(self, used, quota):
        super(QuotaExceededError, self).__init__("quota execeeded {}/{} used".format(used, quota))
