"""Client for the outdoor unit (ODU) at 192.168.254.1.

The ODU runs OpenWRT and exposes ubus over JSON-RPC at /ubus/. Two things about
this endpoint are non-obvious and were found by watching what its own web UI does:

  * calls must be sent as a JSON *array* of RPC objects, even for a single call
  * every method wants ``source_module``/``cid`` style arguments; passing ``{}``
    returns result code 2 (invalid argument) rather than an error message

Result codes come back as ``[code, payload]``: 0 = ok, 2 = bad argument,
3 = no such method.
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request

from .errors import DeviceError

NULL_SESSION = "0" * 32

OK = 0
BAD_ARGUMENT = 2
NO_SUCH_METHOD = 3

# SMS filters, as the firmware numbers them.
ALL_MESSAGES = 10
READ_MESSAGES = 0
UNREAD_MESSAGES = 1

# The radio modes this unit accepts, taken from its own
# js/config/cpe/MC8830/config.js (AUTO_MODES). The stock UI sends exactly these
# strings to nwinfo_set_netselect, so nothing here is a guess.
NET_MODES = {
    "WL_AND_5G": "5G + 4G + 3G (picks whatever is best, safest choice)",
    "LTE_AND_5G": "5G NSA (a 5G carrier added on top of 4G)",
    "Only_5G": "5G standalone (refuses to fall back to 4G)",
    "WCDMA_AND_LTE": "4G + 3G (no 5G)",
    "Only_LTE": "4G only (steadiest, lowest ceiling)",
    "Only_WCDMA": "3G only (last resort)",
}

# router_set_qos's qos_smart_pri_type, taken from the stock UI's own
# speed_allocation_priority_* strings in i18n/Messages_en.properties.
QOS_AUTOMATIC = 0
QOS_GAME = 1
QOS_WEB = 2
QOS_VIDEO = 3
QOS_PRIORITIES = {QOS_AUTOMATIC, QOS_GAME, QOS_WEB, QOS_VIDEO}

# Which mode the dashboard's Optimise picker should reach for, per goal. The ZLT
# client publishes the same map with its own mode names -- see ``zlt.MODE_GOALS``.
MODE_GOALS = {
    "default": "WL_AND_5G",
    "game": "Only_LTE",
    "performance": "LTE_AND_5G",
    "performance_fallback": "Only_LTE",
}

# Everything this hardware supports; the ZLT units support less, so the
# dashboard asks rather than assumes.
CAPABILITIES = {
    "network_mode": True,
    "apn": True,
    "sms": True,
    "qos": True,
    "at": True,
    "auto_reset": True,
    "per_device_bytes": True,
    "single_device": False,
}


def optimise_mode_from_qos(qos):
    """Map a router_get_qos reading back to the Settings tab's picker labels."""
    if not qos:
        return None
    try:
        enabled = bool(int(qos.get("qos_smart_switch") or 0))
        priority = int(qos.get("qos_smart_pri_type") or 0)
    except (TypeError, ValueError):
        return None
    if not enabled:
        return "default"
    if priority == QOS_GAME:
        return "game"
    if priority == QOS_VIDEO:
        return "performance"
    return "default"


class OduError(DeviceError):
    pass


class Odu:
    capabilities = CAPABILITIES
    net_modes = NET_MODES
    mode_goals = MODE_GOALS

    def __init__(self, host, username="admin", password="admin", timeout=10,
                 scheme=None, on_scheme=None):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = None
        # The name of the hash scheme this firmware accepted. Every wrong guess
        # counts towards the device's lockout, so the winner is handed back to
        # the caller through on_scheme and passed in again on the next start.
        self.scheme = scheme
        self.on_scheme = on_scheme
        self._locked_until = 0.0

    # -- transport ---------------------------------------------------------

    def _rpc(self, payload):
        url = "http://%s/ubus/?t=%d" % (self.host, int(time.time() * 1000))
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Referer": "http://%s/index.html" % self.host,
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _anon_call(self, namespace, method, args=None):
        """Call a method that is reachable before a session exists."""
        payload = [{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "call",
            "params": [NULL_SESSION, namespace, method, args or {}],
        }]
        result = self._rpc(payload)[0].get("result")
        if not result or result[0] != OK:
            return {}
        return result[1] if len(result) > 1 else {}

    def _password_candidates(self, salt):
        """Hash schemes used across this firmware family, best guess first.

        The web UI derives the login token from a per-boot salt, but the exact
        derivation differs between builds. Rather than pin one, each candidate is
        offered to the device and the first the firmware accepts is used.

        Each is named, and the name is what gets remembered -- the token itself
        is salt-bound and worthless once the unit reboots, while the derivation
        that produced it stays true.
        """
        sha = lambda text: hashlib.sha256(text.encode()).hexdigest()  # noqa: E731
        upper = sha(self.password).upper()
        lower = sha(self.password)

        named = [
            ("sha-upper-salt-upper", sha(upper + salt.upper()).upper()),
            ("sha-lower-salt", sha(lower + salt).lower()),
            ("sha-upper-salt", sha(upper + salt).upper()),
            ("sha-plain-salt", sha(self.password + salt).upper()),
            ("sha-upper", upper),
            ("base64", base64.b64encode(self.password.encode()).decode()),
            ("plain", self.password),
        ]
        # Once a derivation has worked, trust it exclusively -- cycling through
        # the other six on every login would turn one mistyped password into
        # up to seven real failed attempts, which is plenty to trip the ODU's
        # own lockout. Only probe blind when the scheme is still unknown.
        known = [pair for pair in named if pair[0] == self.scheme]
        return known or named

    def login(self):
        """Authenticate and store the ubus session id.

        Newer builds wrap the password in an RSA/AES envelope, but that path is
        only taken when the device actually serves a certificate. Units that
        return an empty ``crt`` -- including this one -- use the salt alone.
        """
        # Once locked out there is nothing to do but wait, and knocking on the
        # door every second only adds noise, so the deadline is held locally.
        waiting = self._locked_until - time.time()
        if waiting > 0:
            raise OduError(
                "ODU is rate-limiting logins for another %d seconds" % round(waiting))

        info = self._anon_call("zwrt_web", "web_login_info")
        salt = info.get("zte_web_sault", "") or ""

        locked = info.get("login_fail_lock_lefttime")
        if locked:
            try:
                if int(locked) > 0:
                    self._locked_until = time.time() + int(locked)
                    raise OduError(
                        "ODU is rate-limiting logins for another %s seconds" % locked
                    )
            except (TypeError, ValueError):
                pass

        for name, candidate in self._password_candidates(salt):
            out = self._anon_call("zwrt_web", "web_login", {
                "username": self.username,
                "password": candidate,
            })
            session = out.get("ubus_rpc_session")
            if session:
                self.session = session
                if name != self.scheme:
                    self.scheme = name
                    if self.on_scheme:
                        self.on_scheme(name)
                return session

        # Some builds expose the stock OpenWRT login instead of ZTE's wrapper.
        out = self._anon_call("session", "login", {
            "username": self.username,
            "password": self.password,
            "timeout": 3600,
        })
        session = out.get("ubus_rpc_session")
        if session:
            self.session = session
            return session

        raise OduError(
            "ODU login rejected. Check the password in config.json; the device "
            "also locks out briefly after repeated failures."
        )

    def call(self, namespace, method, args=None, _retry=True):
        """Invoke a ubus method, logging in (or back in) as needed."""
        if self.session is None:
            self.login()
        payload = [{
            "jsonrpc": "2.0",
            "id": 1,
            "method": "call",
            "params": [self.session, namespace, method, args or {}],
        }]
        out = self._rpc(payload)
        entry = out[0]

        # An expired session shows up as a JSON-RPC error, not a result code.
        # Anything else that comes back as an "error" (permission denial, a
        # malformed call) is not fixed by a fresh session, so surface it
        # rather than retrying and masking it behind a generic failure.
        if "error" in entry:
            if _retry:
                self.session = None
                return self.call(namespace, method, args, _retry=False)
            message = entry["error"].get("message") or "unknown error"
            raise OduError("%s.%s: %s" % (namespace, method, message))

        result = entry.get("result")
        if not result:
            raise OduError("%s.%s returned no result" % (namespace, method))
        if result[0] == OK:
            return result[1] if len(result) > 1 else {}
        if result[0] == BAD_ARGUMENT and _retry:
            # Some methods reject a stale session with code 2 rather than an error.
            self.session = None
            return self.call(namespace, method, args, _retry=False)
        raise OduError("%s.%s failed with code %d" % (namespace, method, result[0]))

    # -- telemetry ---------------------------------------------------------

    def netinfo(self):
        """Radio state: bands, per-carrier signal, cell identity, operator."""
        return self.call("zte_nwinfo_api", "nwinfo_get_netinfo")

    def carriers(self, netinfo):
        """Per-carrier signal, unpacked from the reading above.

        A method rather than a bare call to :func:`parse_carriers` so the
        collector can ask any supported device the same question.
        """
        return parse_carriers(netinfo)

    def usage(self, kind):
        """Traffic counters. kind is 'session', 'month' or 'total'."""
        type_id = {"session": 1, "month": 2, "total": 3}[kind]
        return self.call("zwrt_data", "get_wwandst",
                         {"source_module": "web", "cid": 1, "type": type_id})

    def data_limit(self):
        return self.call("zwrt_data", "get_wwandst_monthlimit",
                         {"source_module": "web", "cid": 1})

    def wan_status(self):
        return self.call("zwrt_router.api", "router_get_status")

    def sim_info(self):
        return self.call("zwrt_zte_mdm.api", "get_sim_info")

    def sms_list(self, limit=60, tags=ALL_MESSAGES):
        """Stored messages, newest first.

        ``mem_store`` 1 is the modem's own memory; this SIM has no message
        storage of its own (``sms_sim_capability`` is 0), so 1 is the only
        useful value. ``tags`` 10 means every message, 0 read, 1 unread.
        """
        out = self.call("zwrt_wms", "zte_libwms_get_sms_data", {
            "page": 0, "data_per_page": int(limit), "mem_store": 1,
            "tags": tags, "order_by": "order by id desc",
        })
        return out.get("messages") or []

    # -- connection settings the stock web UI hides ------------------------

    PDP_TYPES = {1: "IPv4", 2: "IPv6", 3: "IPv4 and IPv6"}
    AUTH_MODES = {0: "none", 1: "PAP", 2: "CHAP", 3: "PAP or CHAP"}

    def apn_settings(self):
        """Which access point the modem dials with, and what else is on file.

        ``apn_mode`` 1 means a hand-picked profile is in force; 0 means the
        modem picks from its built-in table by operator. Both lists are returned
        so the dashboard can show what the alternative would be.
        """
        mode = self.call("zwrt_apn_object", "get_apn_mode").get("apn_mode")
        return {
            "mode": "manual" if mode == 1 else "automatic",
            "active": self.call("zwrt_apn_object", "get_apn_at_cid", {"cid": 1}),
            "enabled_profile": self.call(
                "zwrt_apn_object", "get_enabled_manu_apn_id").get("profileId"),
            "manual": self.call(
                "zwrt_apn_object", "get_manu_apn_list").get("apnListArray") or [],
            "automatic": self.call(
                "zwrt_apn_object", "get_auto_apn_list").get("apnListArray") or [],
        }

    def lan_settings(self):
        """Its LAN addressing, including what it hands out over DHCP."""
        return self.call("zwrt_router.api", "router_get_dhcp_router")

    def set_apn(self, apn, username="", password="", profile_id=None,
                pdp_type=1, auth_mode=0):
        """Edit a manual APN profile and make it the active one.

        Same object shape the stock UI saves with (``modify_manu_apn``). Only
        the fields a person would actually change are exposed here -- the rest
        of the profile is carried over from what is already on file, so an
        edit cannot silently blank out something unrelated.
        """
        profiles = self.call(
            "zwrt_apn_object", "get_manu_apn_list").get("apnListArray") or []
        if profile_id is None:
            profile_id = self.call(
                "zwrt_apn_object", "get_enabled_manu_apn_id").get("profileId")
        current = next((p for p in profiles if p.get("profileId") == profile_id),
                       profiles[0] if profiles else {})

        profile = dict(current)
        profile.update({
            "wanapn": apn,
            "username": username,
            "password": password,
            "pdpType": int(pdp_type),
            "pppAuthMode": int(auth_mode),
            "isEnable": True,
            "isValid": 1,
        })
        profile.setdefault("profilename", apn)
        profile.setdefault("cid", 1)
        profile.setdefault("extraInt1", 0)
        profile.setdefault("roamingPdpType", int(pdp_type))
        profile["profileId"] = profile_id or profile.get("profileId") or "1"

        self.call("zwrt_apn_object", "modify_manu_apn", profile)
        self.call("zwrt_apn_object", "set_apn_mode", {"apn_mode": 1})
        self.call("zwrt_apn_object", "enable_manu_apn_id",
                 {"profileId": profile["profileId"]})
        return profile

    def sms_capacity(self):
        return self.call("zwrt_wms", "zwrt_wms_get_wms_capacity")

    def at(self, command):
        """Run a raw AT command against the modem and return its response text.

        This is the escape hatch for anything the stock web UI does not expose.
        Only send query forms here -- AT commands can reconfigure the radio.
        """
        out = self.call("zwrt_zte_mdm.api", "run_at_process", {"cmd": command})
        return out.get("at_cmd_result", "")

    # -- writes (guarded by config; see app.py) ----------------------------

    def auto_reset(self):
        """Whether the ODU clears its own monthly counter, and on which day.

        The unit ships with this disabled, which is why its "month" total has
        been running since the day it was installed. Turned on, the device keeps
        a real billing-cycle figure by itself -- no PC required.
        """
        out = self.call("zwrt_data", "get_wwandst_clearday",
                        {"source_module": "web", "cid": 1, "type": 2})
        return {"enabled": bool(int(out.get("enable") or 0)),
                "day": int(out.get("clearday") or 1)}

    def set_auto_reset(self, enabled, day=1):
        return self.call("zwrt_data", "set_wwandst_clearday", {
            "source_module": "web", "cid": 1, "type": 2,
            "enable": 1 if enabled else 0, "clearday": int(day),
        })

    def counters_cleared_on(self):
        """The date the ODU's counters were last zeroed."""
        out = self.call("zwrt_data", "get_wwandst",
                        {"source_module": "web", "cid": 1, "type": 5})
        return out.get("value")

    def set_network_mode(self, mode):
        """mode: one of NET_MODES. Same call the stock UI makes."""
        if mode not in NET_MODES:
            raise OduError("unknown network mode %r" % (mode,))
        return self.call("zte_nwinfo_api", "nwinfo_set_netselect", {"net_select": mode})

    def qos_settings(self):
        """Current QoS Intelligent Allocation state. Empty when switched off."""
        return self.call("zwrt_router.api", "router_get_qos")

    def set_qos(self, enable, priority=QOS_AUTOMATIC):
        """Toggle QoS Intelligent Allocation and its traffic-priority mode.

        ``priority`` is one of the QOS_* constants. Bandwidth caps are a
        separate, unrelated part of this same ubus call; they are left at 0
        (no cap) since nothing here sets them.
        """
        if priority not in QOS_PRIORITIES:
            raise OduError("unknown QoS priority %r" % (priority,))
        return self.call("zwrt_router.api", "router_set_qos", {
            "upload_total_limit_rate": 0,
            "upload_total_limit_unit": 0,
            "download_total_limit_rate": 0,
            "download_total_limit_unit": 0,
            "qos_smart_switch": 1 if enable else 0,
            "qos_smart_pri_type": int(priority),
        })

    def sms_mark_read(self, message_id):
        """tag 0 = read. The unread badge on the ODU's own UI follows this."""
        return self.call("zwrt_wms", "zwrt_wms_modify_tag",
                         {"id": str(message_id), "tag": 0})

    def sms_delete(self, message_id):
        return self.call("zwrt_wms", "zwrt_wms_delete_sms", {"id": str(message_id)})

    def reboot(self):
        return self.call("system", "reboot")

    def start_signal_survey(self):
        return self.call("zte_nwinfo_api", "nwinfo_start_detect_signal_quality")

    def survey_progress(self):
        return self.call("zte_nwinfo_api", "nwinfo_get_progress_and_quality")

    def end_signal_survey(self):
        return self.call("zte_nwinfo_api", "nwinfo_end_detect_signal_quality")


def probe(host, timeout=3):
    """Is there a ZTE-style ubus interface at this address?

    Cheap and unauthenticated, mirroring ``zlt.probe()``: the login-salt call
    answers before any session exists and is specific to this firmware's
    wrapper around ubus, unlike a bare ubus endpoint another device might also
    expose.
    """
    try:
        info = Odu(host, timeout=timeout)._anon_call("zwrt_web", "web_login_info")
    except (OSError, ValueError, IndexError):
        return False
    return "zte_web_sault" in info


# -- parsers ---------------------------------------------------------------

def parse_carriers(netinfo):
    """Expand the packed carrier-aggregation strings into per-carrier dicts.

    ``ltecasig`` is ``rsrp,rsrq,sinr,rssi,?,?`` per carrier and ``lteca`` is
    ``pci,band,?,earfcn,bandwidth``; both are semicolon separated. The two lists
    are not always the same length, so they are zipped defensively.
    """
    def rows(raw):
        return [r for r in (raw or "").split(";") if r.strip()]

    sig_rows = rows(netinfo.get("ltecasig"))
    ca_rows = rows(netinfo.get("lteca"))
    carriers = []

    for index in range(max(len(sig_rows), len(ca_rows))):
        carrier = {"index": index, "primary": index == 0}

        if index < len(sig_rows):
            parts = [p.strip() for p in sig_rows[index].split(",")]
            for name, position in (("rsrp", 0), ("rsrq", 1), ("sinr", 2), ("rssi", 3)):
                if position < len(parts):
                    carrier[name] = _to_float(parts[position])

        if index < len(ca_rows):
            parts = [p.strip() for p in ca_rows[index].split(",")]
            for name, position in (("pci", 0), ("band", 1), ("earfcn", 3), ("bandwidth", 4)):
                if position < len(parts):
                    carrier[name] = _to_float(parts[position])

        carriers.append(carrier)

    return carriers


def _to_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def signal_grade(rsrp):
    """Human label for an RSRP reading, using the usual LTE thresholds."""
    if rsrp is None:
        return "unknown"
    if rsrp >= -80:
        return "excellent"
    if rsrp >= -90:
        return "good"
    if rsrp >= -100:
        return "fair"
    return "poor"
