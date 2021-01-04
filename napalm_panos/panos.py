# Copyright 2016 Dravetech AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
from __future__ import unicode_literals

# std libs
import xmltodict
import json
import pan.xapi
import os.path
import xml.etree
import requests
import requests_toolbelt
from pkg_resources import parse_version
from requests.packages.urllib3.exceptions import InsecureRequestWarning
from datetime import datetime
import logging
import time
import re

# local modules
from napalm.base.utils.string_parsers import convert_uptime_string_seconds
from napalm.base.exceptions import ConnectionException
from napalm.base.exceptions import ReplaceConfigException
from napalm.base.exceptions import MergeConfigException
from napalm.base.exceptions import LockError
from napalm.base.exceptions import UnlockError
from napalm.base import NetworkDriver
from napalm.base.helpers import mac as standardize_mac

from netmiko import ConnectHandler
from netmiko import __version__ as netmiko_version
from pan.xapi import PanXapiError


LOGGER = logging.getLogger(__name__)


class PANOSLock:

    """
    An object to create and release a config- and commit-lock on a PANOS device. Can be used
    as a context-manager.

    The locks are acquired and released using XML API. Locks for config and commit lock
    are obtained and released separately from each other. Both locks are released automatically
    by the device when a commit is made on the device.

    For troubleshooting:
    - The code crashed in a way that the lock could not be removed?
        - Remove the lock manually (CLI, API, Web UI). The lock can only be removed by the
          administrator who set it, or by a superuser.
    - The lock disappeared in the middle of program execution?
        - Did someone do a commit on the device? The locks are removed automatically when
          the administrator who set the locks performs a commit operation on the device.
    """

    LOCK_TYPES = ("config", "commit")
    TAKE_LOCK_API_CMD = (
        "<request><{0}-lock><add><comment>{1}</comment></add></{0}-lock></request>"
    )
    RELEASE_LOCK_API_CMD = "<request><{0}-lock><remove></remove></{0}-lock></request>"

    def __init__(self, device, lock_comment) -> None:
        self._device = device
        self._lock_comment = lock_comment
        self.locked = False

    def __enter__(self):
        self.lock()

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.unlock()

    def lock(self):
        """
        Take lock for config editing and committing new configurations.
        """
        if self.locked:
            LOGGER.debug("Config and commit already locked - skipping")
            return

        for lock_type in self.LOCK_TYPES:
            cmd = self.TAKE_LOCK_API_CMD.format(lock_type, self._lock_comment)
            try:
                self._device.op(cmd=cmd)
            except PanXapiError as e:
                # In case there was any error - also if there already existed some lock,
                # created by our current user - abort the operation. User must manually try
                # again. If for some reason there is a lock that was left by this program
                # but was not removed due to a crash for example, the lock should be manually
                # removed for example by using CLI.
                raise LockError("Failed to aquire {0}-lock: {1}".format(lock_type, str(e)))
            LOGGER.debug("{0}-lock acquired".format(lock_type))
        self.locked = True

    def unlock(self):
        """
        Release lock for config editing and committing new configurations.
        """
        if self.locked is False:
            LOGGER.debug("Config and commit was not locked - skipping release")
            return

        for lock_type in self.LOCK_TYPES:
            cmd = self.RELEASE_LOCK_API_CMD.format(lock_type)
            try:
                self._device.op(cmd=cmd)
            except PanXapiError as e:
                # In case there was any error - also if the lock no longer existed - abort the
                # operation. If lock was removed by someone else, it implies the conf files
                # may have changed in the middle of whatever was being done during the lock this
                # program took.
                #
                # Note: There is a possible edge-case that is not currently handled: Someone
                # removed the lock (possibly made changes and committed them) and then created a
                # new lock, so the lock being removed now is not the one we created. Then its
                # possible that for example when different conf formats are being saved, one
                # of the formats has different data than the others. This case is considered to
                # be too rare for now. If it becomes an issue, it would be possible to inspect
                # the comment of the lock, to determine if it was left by this program or by
                # someone manually (who hopefully left a differing comment). Still not 100%
                # proof, but makes success more likely.
                raise UnlockError("Failed to release {0}-lock: {1}".format(lock_type, str(e)))
            LOGGER.debug("{0}-lock released".format(lock_type))
        self.locked = False


class PANOSDriver(NetworkDriver):
    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.loaded = False
        self.changed = False
        self.device = None
        self.ssh_device = None
        self.ssh_connection = False
        self.merge_config = False
        self._lock_object = None

        if optional_args is None:
            optional_args = {}

        self._session_config_lock = optional_args.get("config_lock", False)
        self._session_config_lock_comment = optional_args.get(
            "config_lock_comment", "NAPALM-managed-lock"
        )

        netmiko_argument_map = {
            "port": None,
            "verbose": False,
            "use_keys": False,
            "key_file": None,
            "ssh_strict": False,
            "system_host_keys": False,
            "alt_host_keys": False,
            "alt_key_file": "",
            "ssh_config_file": None,
        }

        if parse_version(netmiko_version) >= parse_version("2.0.0"):
            netmiko_argument_map["allow_agent"] = False
        elif parse_version(netmiko_version) >= parse_version("1.1.0"):
            netmiko_argument_map["allow_agent"] = False

        # Build dict of any optional Netmiko args
        self.netmiko_optional_args = {}
        for k, v in netmiko_argument_map.items():
            try:
                self.netmiko_optional_args[k] = optional_args[k]
            except KeyError:
                pass
        self.api_key = optional_args.get("api_key", "")

    def open(self):
        try:
            if self.api_key:
                self.device = pan.xapi.PanXapi(
                    hostname=self.hostname, api_key=self.api_key
                )
            else:
                self.device = pan.xapi.PanXapi(
                    hostname=self.hostname,
                    api_username=self.username,
                    api_password=self.password,
                )
        except ConnectionException as e:
            raise ConnectionException(str(e))

        if self._session_config_lock:
            if self._lock_object is None:
                self._lock_object = PANOSLock(
                    self.device, self._session_config_lock_comment
                )
            self._lock_object.lock()

    def _open_ssh(self):
        try:
            self.ssh_device = ConnectHandler(
                device_type="paloalto_panos",
                ip=self.hostname,
                username=self.username,
                password=self.password,
                **self.netmiko_optional_args,
            )
        except ConnectionException as e:
            raise ConnectionException(str(e))

        self.ssh_connection = True

    def close(self):
        if self._session_config_lock and self._lock_object is not None:
            self._lock_object.unlock()
            self._lock_object = None

        self.device = None
        if self.ssh_connection:
            self.ssh_device.disconnect()
            self.ssh_connection = False
            self.ssh_device = None

    def _import_file(self, filename):
        if not self.api_key:
            key = self.device.keygen()
        else:
            key = self.api_key

        params = {"type": "import", "category": "configuration", "key": key}

        path = os.path.basename(filename)

        mef = requests_toolbelt.MultipartEncoder(
            fields={"file": (path, open(filename, "rb"), "application/octet-stream")}
        )

        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
        url = "https://{0}/api/".format(self.hostname)
        request = requests.post(
            url,
            verify=False,
            params=params,
            headers={"Content-Type": mef.content_type},
            data=mef,
        )

        # if something goes wrong just raise an exception
        request.raise_for_status()
        response = xml.etree.ElementTree.fromstring(request.content)

        if response.attrib["status"] == "error":
            return False
        else:
            return path

    def is_alive(self):
        if self.device:
            if self.ssh_connection:
                is_alive = self.ssh_device.remote_conn.transport.is_active()
            else:
                is_alive = True
        else:
            is_alive = False
        return {"is_alive": is_alive}

    def load_replace_candidate(self, filename=None, config=None):
        if config:
            raise ReplaceConfigException("This method requires a config file.")

        elif filename:
            if self.loaded is False:
                if self._save_backup() is False:
                    raise ReplaceConfigException("Error while storing backup config")

            path = self._import_file(filename)
            if path is False:
                msg = "Error while trying to move the config file to the device."
                raise ReplaceConfigException(msg)

            # Let's load the config.
            cmd = "<load><config><from>{0}</from></config></load>".format(path)
            self.device.op(cmd=cmd)

            if self.device.status == "success":
                self.loaded = True
            else:
                raise ReplaceConfigException(
                    "Error while loading config from {0}"
                ).format(path)

        else:
            raise ReplaceConfigException("This method requires a config file.")

    def _get_file_content(self, filename):
        try:
            with open(filename, "r") as f:
                content = f.read()
        except IOError:
            raise MergeConfigException(
                "Error while opening {0}. Make sure "
                "filename is correct.".format(filename)
            )
        return content

    def _send_merge_commands(self, config, file_config):
        """
        Netmiko is being used to push set commands.
        """
        if self.loaded is False:
            if self._save_backup() is False:
                raise MergeConfigException("Error while storing backup " "config.")
        if self.ssh_connection is False:
            self._open_ssh()

        if isinstance(config, str):
            if file_config:
                config = config.splitlines()
            else:
                config = str(config).splitlines()

        self.ssh_device.send_config_set(config)
        self.loaded = True
        self.merge_config = True

    def _get_candidate(self):
        candidate_command = "<show><config><candidate></candidate></config></show>"
        self.device.op(cmd=candidate_command)
        candidate = str(self.device.xml_root())
        return candidate

    def _get_running(self):
        self.device.show()
        running = str(self.device.xml_root())
        return running

    def get_config(self, retrieve="all", full=False, sanitized=False):
        """
        Full and Sanitized is not supported, need to apply to pass tests. It
        is not clear to me if this construct exists in panos
        """
        if full:
            raise NotImplementedError(
                "Full config is not implemented for this platform"
            )
        if sanitized:
            raise NotImplementedError("Sanitized is not implemented for this platform")
        configs = {}
        running = ""
        candidate = ""
        startup = ""

        if retrieve == "all":
            running = self._get_running()
            candidate = self._get_candidate()
        elif retrieve == "running":
            running = self._get_running()
        elif retrieve == "candidate":
            candidate = self._get_candidate()

        configs["running"] = running
        configs["candidate"] = candidate
        configs["startup"] = startup

        return configs

    def load_merge_candidate(self, filename=None, config=None):
        if filename:
            file_config = True
            content = self._get_file_content(filename)
            config = content.splitlines()
            self._send_merge_commands(config, file_config)

        elif config:
            file_config = False
            self._send_merge_commands(config, file_config)

        else:
            raise MergeConfigException(
                "You must provide either a file " "or a set-format string"
            )

    def compare_config(self):
        """
        Netmiko is being used to obtain config diffs because pan-python
        doesn't support the needed command.
        """
        if self.ssh_connection is False:
            self._open_ssh()

        self.ssh_device.exit_config_mode()
        diff = self.ssh_device.send_command("show config diff")
        return diff.strip()

    def _save_backup(self):
        self.backup_file = "config_{0}.xml".format(
            str(datetime.now().date()).replace(" ", "_")
        )
        backup_command = "<save><config><to>{0}</to></config></save>".format(
            self.backup_file
        )

        self.device.op(cmd=backup_command)
        if self.device.status == "success":
            return True
        else:
            return False

    def commit_config(self, message=""):
        """
        Netmiko is being used to commit the configuration because it takes
        a better care of results compared to pan-python.
        """
        if self.loaded:
            if self.ssh_connection is False:
                self._open_ssh()
            try:
                self.ssh_device.commit(comment=message)
                time.sleep(3)
                self.loaded = False
                self.changed = True
            except:  # noqa
                if self.merge_config:
                    raise MergeConfigException("Error while commiting config")
                else:
                    raise ReplaceConfigException("Error while commiting config")
        else:
            raise ReplaceConfigException("No config loaded.")

        if self._session_config_lock:
            # PANOS device releases locks automatically upon commit. Take lock again, and let
            # it be explicitly released by the driver when the driver is being closed.
            self._lock_object.locked = False  # Allow lock to be taken again.
            self._lock_object.lock()

    def discard_config(self):
        if self.loaded:
            discard_cmd = "<load><config><from>{0}</from></config></load>".format(
                self.backup_file
            )
            self.device.op(cmd=discard_cmd)

            if self.device.status == "success":
                self.loaded = False
                self.merge_config = False
            else:
                raise ReplaceConfigException("Error while loading backup config.")

    def rollback(self):
        """
        Netmiko is being used to commit the rollback configuration because
        it takes a better care of results compared to pan-python.
        """
        if self.changed:
            rollback_cmd = "<load><config><from>{0}</from></config></load>".format(
                self.backup_file
            )
            self.device.op(cmd=rollback_cmd)
            time.sleep(5)

            if self.ssh_connection is False:
                self._open_ssh()
            try:
                self.ssh_device.commit()
                self.loaded = False
                self.changed = False
                self.merge_config = False
            except:  # noqa
                ReplaceConfigException("Error while loading backup config")

    def _extract_interface_list(self):
        self.device.op(cmd="<show><interface>all</interface></show>")
        interfaces_xml = xmltodict.parse(self.device.xml_root())
        interfaces_json = json.dumps(interfaces_xml["response"]["result"])
        interfaces = json.loads(interfaces_json)

        interface_set = set()

        for entry in interfaces.values():
            for entry_contents in entry.values():
                if isinstance(entry_contents, dict):
                    # If only 1 interface is listed, xmltodict returns a dictionary, otherwise
                    # it returns a list of dictionaries.
                    entry_contents = [entry_contents]
                for intf in entry_contents:
                    interface_set.add(intf["name"])

        return list(interface_set)

    def get_facts(self):
        facts = {}

        try:
            self.device.op(cmd="<show><system><info></info></system></show>")
            system_info_xml = xmltodict.parse(self.device.xml_root())
            system_info_json = json.dumps(
                system_info_xml["response"]["result"]["system"]
            )
            system_info = json.loads(system_info_json)
        except AttributeError:
            system_info = {}

        if system_info:
            facts["hostname"] = system_info["hostname"]
            facts["vendor"] = "Palo Alto Networks"
            facts["uptime"] = int(convert_uptime_string_seconds(system_info["uptime"]))
            facts["os_version"] = system_info["sw-version"]
            facts["serial_number"] = system_info["serial"]
            facts["model"] = system_info["model"]
            facts["fqdn"] = "N/A"
            facts["interface_list"] = self._extract_interface_list()

            facts["interface_list"].sort()

        return facts

    def get_lldp_neighbors(self):
        """Return LLDP neighbors details."""

        neighbors = {}

        cmd = "<show><lldp><neighbors>all</neighbors></lldp></show>"
        try:
            self.device.op(cmd=cmd)
            lldp_table_xml = xmltodict.parse(self.device.xml_root())
            lldp_table_json = json.dumps(lldp_table_xml["response"]["result"]["entry"])
            lldp_table = json.loads(lldp_table_json)
        except AttributeError:
            lldp_table = []

        if isinstance(lldp_table, dict):
            # If only 1 interface is listed, xmltodict returns a dictionary, otherwise
            # it returns a list of dictionaries.
            lldp_table = [lldp_table]

        for lldp_item in lldp_table:

            local_int = lldp_item["@name"]

            if local_int not in neighbors.keys():
                neighbors[local_int] = []
            try:
                lldp_neighs = lldp_item.get("neighbors").get("entry")
            except AttributeError:
                lldp_neighs = ""
            if isinstance(lldp_neighs, dict):
                lldp_neighs = [lldp_neighs]

            for neighbor in lldp_neighs:
                n = {}
                n["hostname"] = neighbor["system-name"]
                n["port"] = neighbor["port-id"]
                neighbors[local_int].append(n)
        return neighbors

    def get_route_to(self, destination="", protocol="", longer=False):
        """Return route details to a specific destination, learned from a certain protocol."""

        if longer:
            raise NotImplementedError("Longer is not implemented for this platform")

        # Note, it should be possible to query the FIB:
        # "<show><routing><fib></fib></routing></show>"
        # To add informations to this getter
        routes = {}

        if destination:
            destination = "<destination>{0}</destination>".format(destination)
        if protocol:
            protocol = "<type>{0}</type>".format(protocol)

        cmd = "<show><routing><route>{0}{1}</route></routing></show>".format(
            protocol, destination
        )
        try:
            self.device.op(cmd=cmd)
            routes_table_xml = xmltodict.parse(self.device.xml_root())
            routes_table_json = json.dumps(
                routes_table_xml["response"]["result"]["entry"]
            )
            routes_table = json.loads(routes_table_json)
        except (AttributeError, KeyError):
            routes_table = []

        if isinstance(routes_table, dict):
            routes_table = [routes_table]

        for route in routes_table:
            d = {
                "current_active": False,
                "last_active": False,
                "age": -1,
                "next_hop": "",
                "protocol": "",
                "outgoing_interface": "",
                "preference": -1,
                "inactive_reason": "",
                "routing_table": "default",
                "selected_next_hop": False,
                "protocol_attributes": {},
            }
            destination = route["destination"]
            flags = route["flags"]

            if "A" in flags:
                d["current_active"] = True
            else:
                d["current_active"] = False
            if "C" in flags:
                d["protocol"] = "connect"
            if "S" in flags:
                d["protocol"] = "static"
            if "R" in flags:
                d["protocol"] = "rip"
            if "R" in flags:
                d["protocol"] = "rip"
            if "O" in flags:
                d["protocol"] = "ospf"
            if "B" in flags:
                d["protocol"] = "bgp"
            if "H" in flags:
                d["protocol"] = "host"
            if route["age"] is not None:
                d["age"] = int(route["age"])
            if route["nexthop"] is not None:
                d["next_hop"] = route["nexthop"]
            if route["interface"] is not None:
                d["outgoing_interface"] = route["interface"]
            if route["metric"] is not None:
                d["preference"] = int(route["metric"])
            if route["virtual-router"] is not None:
                d["routing_table"] = route["virtual-router"]

            if destination not in routes.keys():
                routes[destination] = []
            routes[destination].append(d)

        return routes

    def get_interfaces(self):
        SUBIF_DEFAULTS = {
            "is_up": True,
            "is_enabled": True,
            "speed": 0,
            "last_flapped": -1.0,
            "mac_address": "",
            "mtu": 0,
            "description": "N/A",
        }
        interface_pattern = re.compile(
            r"(ethernet\d+/\d+\.\d+)|(ae\d+\.\d+)|(loopback\.)|(tunnel\.)|(vlan\.)"
        )
        interface_dict = {}
        interface_list = self._extract_interface_list()

        for intf in interface_list:
            interface = {}
            cmd = "<show><interface>{0}</interface></show>".format(intf)

            try:
                self.device.op(cmd=cmd)
                interface_info_xml = xmltodict.parse(self.device.xml_root())
                interface_info_json = json.dumps(
                    interface_info_xml["response"]["result"]["hw"]
                )
                interface_info = json.loads(interface_info_json)
            except KeyError as err:
                if interface_pattern.search(intf) and "hw" in str(err):
                    # physical/ae/tunnel/loopback sub-ifs don't return a 'hw' key
                    interface_dict[intf] = SUBIF_DEFAULTS
                    continue
                raise

            interface["is_up"] = interface_info.get("state") == "up"

            conf_state = interface_info.get("state_c")
            if conf_state == "down":
                interface["is_enabled"] = False
            elif conf_state in ("up", "auto"):
                interface["is_enabled"] = True
            else:
                msg = "Unknown configured state {} for interface {}".format(
                    conf_state, intf
                )
                raise RuntimeError(msg)

            interface["last_flapped"] = -1.0
            interface["mtu"] = 0
            interface["speed"] = interface_info.get("speed")
            # Loopback and down interfaces
            if interface["speed"] in ("[n/a]", "unknown"):
                interface["speed"] = 0
            else:
                interface["speed"] = int(interface["speed"])
            interface["mac_address"] = standardize_mac(interface_info.get("mac"))
            interface["description"] = "N/A"
            interface_dict[intf] = interface

        return interface_dict

    def get_interfaces_ip(self):
        """Return IP interface data."""

        def extract_ip_info(parsed_intf_dict):
            """
            IPv4:
              - Primary IP is in the '<ip>' tag. If no v4 is configured the return value is 'N/A'.
              - Secondary IP's are in '<addr>'. If no secondaries, this field is not returned by
                the xmltodict.parse() method.

            IPv6:
              - All addresses are returned in '<addr6>'. If no v6 configured, this is not returned
                either by xmltodict.parse().

            Example of XML response for an intf with multiple IPv4 and IPv6 addresses:

            <response status="success">
              <result>
                <ifnet>
                  <entry>
                    <name>ethernet1/5</name>
                    <zone/>
                    <fwd>N/A</fwd>
                    <vsys>1</vsys>
                    <dyn-addr/>
                    <addr6>
                      <member>fe80::d61d:71ff:fed8:fe14/64</member>
                      <member>2001::1234/120</member>
                    </addr6>
                    <tag>0</tag>
                    <ip>169.254.0.1/30</ip>
                    <id>20</id>
                    <addr>
                      <member>1.1.1.1/28</member>
                    </addr>
                  </entry>
                  {...}
                </ifnet>
                <hw>
                  {...}
                </hw>
              </result>
            </response>
            """
            intf = parsed_intf_dict["name"]
            _ip_info = {intf: {}}

            v4_ip = parsed_intf_dict.get("ip")
            secondary_v4_ip = parsed_intf_dict.get("addr")
            v6_ip = parsed_intf_dict.get("addr6")

            if v4_ip != "N/A":
                address, pref = v4_ip.split("/")
                _ip_info[intf].setdefault("ipv4", {})[address] = {
                    "prefix_length": int(pref)
                }

            if secondary_v4_ip is not None:
                members = secondary_v4_ip["member"]
                if not isinstance(members, list):
                    # If only 1 secondary IP is present, xmltodict converts field to a string, else
                    # it converts it to a list of strings.
                    members = [members]
                for address in members:
                    address, pref = address.split("/")
                    _ip_info[intf].setdefault("ipv4", {})[address] = {
                        "prefix_length": int(pref)
                    }

            if v6_ip is not None:
                members = v6_ip["member"]
                if not isinstance(members, list):
                    # Same "1 vs many -> string vs list of strings" comment.
                    members = [members]
                for address in members:
                    address, pref = address.split("/")
                    _ip_info[intf].setdefault("ipv6", {})[address] = {
                        "prefix_length": int(pref)
                    }

            # Reset dictionary if no addresses were found.
            if _ip_info == {intf: {}}:
                _ip_info = {}

            return _ip_info

        ip_interfaces = {}
        cmd = "<show><interface>all</interface></show>"

        self.device.op(cmd=cmd)
        interface_info_xml = xmltodict.parse(self.device.xml_root())
        interface_info_json = json.dumps(
            interface_info_xml["response"]["result"]["ifnet"]["entry"]
        )
        interface_info = json.loads(interface_info_json)

        if isinstance(interface_info, dict):
            # Same "1 vs many -> dict vs list of dicts" comment.
            interface_info = [interface_info]

        for interface_dict in interface_info:
            ip_info = extract_ip_info(interface_dict)
            if ip_info:
                ip_interfaces.update(ip_info)

        return ip_interfaces
