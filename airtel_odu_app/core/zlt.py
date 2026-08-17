"""Client for the ZLT family of CPEs (X17U and relatives, Tozed Kangwei).

Same job as ``odu.py`` and ``router.py`` together, for hardware that works
nothing like the ZTE pair:

  * one box, one address, one login -- the outdoor unit and the indoor router
    answer on the same web interface, so ``ZltOdu`` and ``ZltRouter`` are two
    faces of a single session rather than two devices;
  * everything is a POST of JSON to ``/cgi-bin/http.cgi``, where the endpoint is
    picked by a ``cmd`` UUID rather than a path. The UUIDs below were read off
    the unit's own web app; the numeric ``cmd`` echoed in each reply is the
    firmware's internal id for the same call.

The two quirks worth knowing before changing anything here:

  * ``method`` is a *field in the body*, not the HTTP verb. Every request is an
    HTTP POST; ``method: "GET"`` means "read" and ``method: "POST"`` means
    "write", and a write additionally needs a one-shot ``token`` fetched
    immediately beforehand.
  * a lapsed session is not an HTTP status. It comes back as ``success: false``
    with ``message: "NO_AUTH"`` (or ``LOGIN_TIMEOUT``), which is why every call
    goes through :meth:`ZltSession.call` rather than :meth:`ZltSession.rpc`.

The unit exposes no per-client byte counters and no QoS on the ordinary admin
account, so those two features report themselves as unsupported rather than
guessing -- see ``CAPABILITIES`` below and how ``app.py`` passes it to the UI.
"""

import base64
import calendar
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from .errors import DeviceError

# -- the firmware's endpoints, by the name its own web app gives them --------

CMD_LOGIN_TOKEN = "3830c61a-620d-47da-ae47-33d8401401c4"
CMD_LOGIN = "d2aa9843-494b-4947-9621-a46ec652ecd9"
CMD_LOGOUT = "677d89ca-2e5c-4481-81e3-cb6965ae77da"
CMD_WRITE_TOKEN = "f3b70f2f-8721-48c4-87ec-22d8c92dd3c9"
CMD_PRELOGIN = "835e31b3-a45a-4504-80df-586bf8a509aa"

CMD_CONFIG = "55f29f9b-20cd-4d72-ab20-63ba0b4d2a7a"
CMD_LOOP = "2ee26212-96cc-45d3-8f0d-808e4cde884a"
CMD_STATUS = "f3e328b1-c743-4aaf-be88-fdb5e32d7e51"
CMD_NETINFO = "89af35c9-b448-4fc7-a477-828a3e9467f8"
CMD_DEVICE_INFO = "ece6b6d4-61c7-4dad-af23-c8249c75c58c"
CMD_STATIONS = "5332f5ee-5be9-4843-b85f-1b251aa5f4ff"
CMD_LAN = "97fc0d9d-ae10-48ae-91fd-79adcce320cf"
CMD_TRAFFIC = "24959b3c-291a-47ff-83e6-bcce57de99a3"
CMD_NETWORK_MODE = "371dce21-2ba8-48e3-befe-79337182358f"
CMD_APN_LIST = "870ad941-ddef-4c52-9c36-a6d8f9c41c19"
CMD_APN_GLOBAL = "a23c4e81-a76e-4192-8452-d7acfe18c307"
CMD_QOS = "0b1734b4-6320-4798-b8f2-2dd7868ce513"
CMD_AT = "9703a2dc-bd34-43b5-a15c-4491638e9e32"
CMD_REBOOT = "7a9cfe11-78bb-43aa-8041-4bcb0b839565"
CMD_SMS_LIST = "ee71744e-50b4-4d2a-9c2d-0c4c7b968fc5"
CMD_SMS_DELETE = "00ed1003-91d6-4877-aa38-22b1c99eaeca"
CMD_SMS_CAPACITY = "fcb360fb-2e3c-4928-ac85-4815580c468b"

# ``subcmd`` on the SMS calls selects a mailbox: 0 inbox, 1 sent.
INBOX = 0
SENT = 1

# What this hardware can and cannot do, next to the ZTE pair. Read by the
# dashboard so unsupported controls are hidden rather than left to fail.
CAPABILITIES = {
    "network_mode": True,
    "apn": True,
    "sms": True,
    # No QoS on this firmware's admin account -- router_get_qos answers with
    # every field blank, so there is nothing to switch.
    "qos": False,
    # The AT passthrough exists but is refused (LIMITED_ACCESS) for anything
    # below a root-level account.
    "at": False,
    # The monthly counter always rolls over on its start day; it cannot be
    # turned off, only moved.
    "auto_reset": False,
    # dhcp_list_info reports a per-client ``flow`` field that this firmware
    # never fills in, so per-device totals come from our own ledger only.
    "per_device_bytes": False,
    "single_device": True,
}

# The radio modes the firmware understands, keyed the way it wants them (a hex
# string). Which of these a given unit actually offers is decided by the
# ``networkMode_opt`` bitmask it reports -- see :func:`modes_for`. The bit each
# mode sits on is the index the stock web app uses.
MODE_BITS = {
    0: "1", 1: "2", 2: "3", 3: "4", 4: "40", 5: "20", 6: "6", 7: "7",
    8: "C", 9: "10", 10: "1C", 11: "E", 12: "F", 13: "1E", 14: "1F", 15: "14",
}

MODE_LABELS = {
    "1": "2G only (last resort)",
    "2": "3G only (last resort)",
    "3": "3G + 2G (no 4G)",
    "4": "4G only (steadiest, lowest ceiling)",
    "20": "4G FDD only (one half of 4G, for a stubborn cell)",
    "40": "4G TDD only (the other half of 4G)",
    "6": "4G + 3G (no 5G)",
    "7": "4G + 3G + 2G (no 5G, widest fallback)",
    "C": "5G NSA only (a 5G carrier added on top of 4G)",
    "10": "5G standalone only (refuses to fall back to 4G)",
    "14": "5G SA + 4G",
    "1C": "5G SA/NSA + 4G (picks whatever is best)",
    "E": "5G + 4G + 3G (picks whatever is best, safest choice)",
    "F": "5G + 4G + 3G + 2G (widest fallback)",
    "1E": "5G SA/NSA + 4G + 3G (picks whatever is best)",
    "1F": "5G SA/NSA + 4G + 3G + 2G (widest fallback)",
}

# Preference order behind the dashboard's Optimise picker, best first. Whichever
# of these the unit actually offers wins; see :func:`goals_for`.
MODE_GOALS = {
    "default": ["1E", "1C", "E", "F", "1F", "C", "6", "7", "4"],
    "game": ["4", "40", "6", "7", "2"],
    "performance": ["1C", "C", "1E", "E", "10", "14"],
    "performance_fallback": ["4", "40", "6", "7"],
}

# network_type_str as this firmware writes it, mapped onto the tokens the
# dashboard already labels (the ZTE units' vocabulary).
TECH_NAMES = [
    (re.compile(r"5G.*NSA|NSA", re.I), "ENDC"),
    (re.compile(r"5G", re.I), "NR5G"),
    (re.compile(r"LTE|4G", re.I), "LTE"),
    (re.compile(r"WCDMA|UMTS|HSPA|3G", re.I), "WCDMA"),
    (re.compile(r"GSM|EDGE|GPRS|2G", re.I), "GSM"),
    (re.compile(r"no service|limited", re.I), "NO_SERVICE"),
]

MIB = 1024 ** 2


class ZltError(DeviceError):
    pass


# -- transport --------------------------------------------------------------

class ZltSession:
    """One logged-in conversation with the unit, shared by both faces of it."""

    def __init__(self, host, username="admin", password="admin", timeout=10):
        self.host = host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session_id = ""
        self._config = None
        self._utc_offset = None
        # Both faces of the unit share this session across the collector's two
        # threads. The device is not fast enough for that to be worth
        # overlapping, and letting two of them log in at once would have the
        # firmware invalidate one session with the other.
        self._lock = threading.RLock()

    @property
    def url(self):
        return "http://%s/cgi-bin/http.cgi" % self.host

    def rpc(self, cmd, method="GET", **params):
        """One request, no session handling. Use :meth:`call` instead."""
        body = dict(params)
        body.update({"cmd": cmd, "method": method, "sessionId": self.session_id})
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            # Referer and nothing else. An ``Origin`` header -- harmless on the
            # ZTE units, and the obvious thing to send alongside it -- makes
            # this firmware treat the request as cross-site: it answers NO_AUTH
            # and throws the session away, so the next call has to log in again.
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "Referer": "http://%s/index.html" % self.host,
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8", "replace")
        # Some builds prefix the JSON with stray bytes; the stock web app skips
        # to the first brace rather than trusting the whole body, so do the same.
        start = raw.find("{")
        if start < 0:
            raise ZltError("unparseable response for cmd %s" % cmd)
        try:
            return json.loads(raw[start:])
        except ValueError:
            raise ZltError("unparseable response for cmd %s" % cmd)

    # -- auth ------------------------------------------------------------

    def login(self):
        """Authenticate and keep the session id.

        The password is never sent: the unit hands out a per-attempt token and
        wants sha256(token + password) back. The session id it then returns is
        carried in the *body* of every later call -- the unit also sets it as a
        cookie, but the body is what it actually checks.

        Worth knowing: this firmware keeps exactly one session per account and a
        fresh login evicts whatever came before. Left alone the session lasts
        indefinitely, but opening the unit's own web page in a browser will log
        this app out, and vice versa. Nothing breaks either way -- every call
        re-authenticates when it finds itself locked out.
        """
        self.session_id = ""
        try:
            self.rpc(CMD_PRELOGIN)          # the stock UI's own warm-up call
        except (OSError, ZltError):
            pass

        token = (self.rpc(CMD_LOGIN_TOKEN) or {}).get("token")
        if not token:
            raise ZltError("the device would not issue a login token")

        # The stock UI makes up the session id itself and the firmware honours
        # it until it answers with one of its own.
        self.session_id = _random_hex()
        out = self.rpc(
            CMD_LOGIN, "POST",
            username=self.username,
            passwd=hashlib.sha256((token + self.password).encode()).hexdigest(),
            token=token,
        )

        if out.get("sessionId"):
            self.session_id = out["sessionId"]
            self._config = None
            return self.session_id

        self.session_id = ""
        raise ZltError(_login_failure(out))

    def logged_in(self):
        return bool(self.session_id)

    def logout(self):
        if self.session_id:
            try:
                self.call(CMD_LOGOUT, "POST", _retry=False)
            except (OSError, ZltError):
                pass
        self.session_id = ""

    # -- calls -----------------------------------------------------------

    def call(self, cmd, method="GET", params=None, _retry=True):
        """Invoke an endpoint, logging in (or back in) as needed."""
        with self._lock:
            return self._call(cmd, method, params, _retry)

    def _call(self, cmd, method, params, _retry):
        if not self.session_id:
            self.login()

        params = dict(params or {})
        if method == "POST":
            params.setdefault("token", self._write_token())

        out = self.rpc(cmd, method, **params)
        if out.get("success"):
            return out

        message = str(out.get("message") or "")
        # All three mean the same thing: something else logged in and took the
        # session with it. "Invalid CSRF Token" is the flavour a write gets,
        # since its token was minted against the session that just went away.
        if message in ("NO_AUTH", "LOGIN_TIMEOUT", "Invalid CSRF Token") and _retry:
            self.login()
            # The write token was minted against the session that just lapsed,
            # so it goes with it -- the retry fetches a fresh one.
            params.pop("token", None)
            return self._call(cmd, method, params, _retry=False)
        if message == "LIMITED_ACCESS":
            raise ZltError(
                "the device refused this to the '%s' account -- it needs a "
                "higher-privilege login" % self.username)
        raise ZltError("cmd %s failed%s" % (cmd, ": " + message if message else ""))

    def get(self, cmd, **params):
        return self.call(cmd, "GET", params)

    def post(self, cmd, **params):
        return self.call(cmd, "POST", params)

    def _write_token(self):
        """Writes carry a CSRF token, and it really is one-shot.

        The firmware rotates it the moment a write uses it, so it is fetched
        immediately before each one rather than cached -- reusing a spent token
        costs the whole session, not just the call.

        Fetched through :meth:`_call`, not :meth:`rpc`: this endpoint needs a
        live session like any other, and a lapsed one here would otherwise turn
        into an empty token and a baffling "Invalid CSRF Token" on the write
        that follows, instead of a quiet re-login.
        """
        token = (self._call(CMD_WRITE_TOKEN, "GET", None, True) or {}).get("token")
        if not token:
            raise ZltError("the device would not issue a token for this change")
        return token

    # -- things worth asking only once -----------------------------------

    def config(self, refresh=False):
        """The unit's own description of itself: model, firmware, what it can do."""
        if self._config is None or refresh:
            self._config = self.get(CMD_CONFIG)
        return self._config

    def model(self):
        config = self.config()
        return (config.get("real_device") or config.get("board_type")
                or config.get("device_module") or "ZLT")

    def utc_offset(self):
        """Seconds east of UTC, as the unit's own clock is set.

        Stored messages carry a wall-clock time with no zone, so this is the
        only way to turn one into a real timestamp. Rounded to a quarter hour,
        which is the finest granularity any real zone uses.
        """
        if self._utc_offset is None:
            systime = (self.get(CMD_DEVICE_INFO) or {}).get("systime") or ""
            try:
                stamp = time.strptime(systime, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                self._utc_offset = 0
            else:
                drift = calendar.timegm(stamp) - time.time()
                self._utc_offset = int(round(drift / 900.0)) * 900
        return self._utc_offset


# -- the outdoor-unit face --------------------------------------------------

class ZltOdu:
    """The radio, the data counters, the SIM and its messages."""

    capabilities = CAPABILITIES

    def __init__(self, link):
        self.link = link
        # Live speeds are worked out here rather than read: the unit publishes
        # cumulative WAN byte counters and no rate, so each sample is compared
        # with the one before it.
        self._last_sample = None
        self._rates = (0, 0)
        # Nor is there a per-session counter -- only a running monthly one and
        # the time the current connection came up. Noting where the monthly
        # counter stood when this connection started gives the session figure.
        self._session_base = None
        self._session_since = None
        self._netinfo = None
        self._netinfo_at = 0.0
        # The selected radio mode lives behind its own call and only changes
        # when somebody changes it, so it is not worth a request per reading.
        self._mode = None
        self._mode_at = 0.0

    # -- session, in the shape app.py expects of the ZTE client -----------

    @property
    def password(self):
        return self.link.password

    @password.setter
    def password(self, value):
        self.link.password = value

    @property
    def session(self):
        return self.link.session_id or None

    @session.setter
    def session(self, value):
        self.link.session_id = value or ""

    def login(self):
        return self.link.login()

    # -- radio -----------------------------------------------------------

    def netinfo(self, max_age=0.8):
        """Radio state, with the field names the rest of the app already uses.

        The unit's own names are left in place alongside, so nothing is lost --
        only the handful the dashboard reads are aliased.

        Everything the fast loop needs comes from this one call, so a reading
        is reused for ``max_age`` seconds rather than fetched three times a
        second.
        """
        now = time.time()
        if self._netinfo is not None and now - self._netinfo_at < max_age:
            return self._netinfo

        raw = self.link.get(CMD_NETINFO)
        info = dict(raw)
        info.update({
            "network_provider": raw.get("network_operator", ""),
            "network_provider_fullname": raw.get("network_operator", ""),
            "network_type": _tech(raw.get("network_type_str")),
            "network_type_raw": raw.get("network_type_str", ""),
            "wan_active_band": _band_label(raw),
            "wan_active_channel": _int(raw.get("FREQ")),
            "cell_id": _int(raw.get("CELL_ID"), base=16),
            "lte_pci": _int(raw.get("PCI")),
            "lte_band": _band_name(raw.get("currentband")),
            "lte_rsrp": _float(raw.get("RSRP")),
            "lte_snr": _float(raw.get("SINR")),
            "nr5g_action_band": _band_name(raw.get("currentband_5g"), "n"),
            "nr5g_bandwidth": raw.get("bandwidth_5g", ""),
            "nr5g_rsrp": raw.get("RSRP_5G", ""),
            "net_select": self.current_mode(),
        })
        self._netinfo = info
        self._netinfo_at = now
        self._sample(raw, now)
        return info

    def carriers(self, netinfo):
        """One row per frequency block in use, strongest-anchor first.

        The anchor comes first because that is the carrier every reading in the
        dashboard is graded against; on this hardware in NSA that is the 4G
        cell, with the 5G carrier riding on top of it.
        """
        carriers = []

        if _float(netinfo.get("RSRP")) is not None:
            carriers.append(_carrier(len(carriers), netinfo, "", ""))
        if _float(netinfo.get("RSRP_5G")) is not None:
            carriers.append(_carrier(len(carriers), netinfo, "_5G", "_5g"))

        # Extra 4G carriers, when the unit reports aggregation: each field is a
        # comma-separated list, one entry per added carrier.
        extras = _split(netinfo.get("ca_freq"))
        for index, earfcn in enumerate(extras):
            carrier = {"index": len(carriers), "primary": False,
                       "earfcn": _float(earfcn)}
            for name, field in (("pci", "ca_pci"), ("rsrp", "ca_rsrp"),
                                ("rsrq", "ca_rsrq"), ("rssi", "ca_rssi"),
                                ("bandwidth", "ca_bandwidth")):
                values = _split(netinfo.get(field))
                if index < len(values):
                    carrier[name] = _float(values[index])
            carriers.append(carrier)

        for index, carrier in enumerate(carriers):
            carrier["index"] = index
            carrier["primary"] = index == 0
        return carriers

    def wan_status(self):
        info = self.netinfo()
        ip = info.get("wan_ip") or ""
        online = str(info.get("wan_network_status")) == "1" and ip not in ("", "0.0.0.0")
        return {
            "current_wan_status": "ipv4_connected" if online else "disconnected",
            "wan_ipaddr": ip,
            "wan_apn": info.get("apn_name", ""),
            "wan_dns": info.get("wan_dns", ""),
        }

    def sim_info(self):
        info = self.link.get(CMD_DEVICE_INFO)
        return {
            "imsi": info.get("IMSI", ""),
            "iccid": info.get("ICCID", ""),
            "imei": info.get("module_imei", ""),
            "msisdn": info.get("device_msisdn", ""),
            "sim_status": self.link.config(refresh=True).get("sim_status", ""),
        }

    # -- counters --------------------------------------------------------

    def usage(self, kind):
        """Traffic counters. kind is 'session', 'month' or 'total'.

        The unit keeps a monthly figure in bytes (exactly, on the WAN interface)
        and a lifetime one in megabytes. There is no session counter, so that
        one is measured from where the monthly counter stood when the current
        connection came up.
        """
        info = self.netinfo()
        month_down = _int(info.get("wan_rx_bytes")) or 0
        month_up = _int(info.get("wan_tx_bytes")) or 0

        if kind == "month":
            return {"month_rx_bytes": month_down, "month_tx_bytes": month_up}

        if kind == "total":
            return {
                "total_rx_bytes": _mib(info.get("flow_dl"), month_down),
                "total_tx_bytes": _mib(info.get("flow_ul"), month_up),
            }

        if kind != "session":
            raise ZltError("unknown usage window %r" % (kind,))

        base_down, base_up = self._session_base or (month_down, month_up)
        down_rate, up_rate = self._rates
        return {
            "real_rx_bytes": max(0, month_down - base_down),
            "real_tx_bytes": max(0, month_up - base_up),
            "real_rx_speed": down_rate,
            "real_tx_speed": up_rate,
            "real_time": _int(info.get("data_connect_time")) or 0,
        }

    def _sample(self, raw, now):
        """Turn successive counter readings into a rate, in bytes per second."""
        down = _int(raw.get("wan_rx_bytes")) or 0
        up = _int(raw.get("wan_tx_bytes")) or 0

        since = raw.get("onlineTime") or ""
        if since != self._session_since or self._session_base is None:
            self._session_since = since
            self._session_base = (down, up)
        elif down < self._session_base[0] or up < self._session_base[1]:
            # The monthly counter rolled over underneath us.
            self._session_base = (down, up)

        last = self._last_sample
        if not last:
            self._last_sample = (now, down, up)
            return
        span = now - last[0]
        # Two readings a hair apart -- both poll threads landing together --
        # would turn a few stray bytes into a headline speed, so the older
        # sample is kept until there is a real interval to divide by.
        if span < 0.3:
            return
        self._last_sample = (now, down, up)
        if down < last[1] or up < last[2]:
            self._rates = (0, 0)
            return
        self._rates = (int((down - last[1]) / span), int((up - last[2]) / span))

    # -- settings the dashboard reads and writes -------------------------

    @property
    def net_modes(self):
        """The radio modes this particular unit offers, key -> label.

        Read from the device, but never at the cost of an error: a unit that is
        not answering yet falls back to the three modes every one of these
        modems has, so the dashboard can still draw its picker.
        """
        try:
            bitmask = self.link.config().get("networkMode_opt")
        except (DeviceError, OSError):
            bitmask = None
        return modes_for(bitmask)

    @property
    def mode_goals(self):
        return goals_for(self.net_modes)

    def current_mode(self, max_age=60):
        if self._mode is None or time.time() - self._mode_at > max_age:
            self._mode = (self.link.get(CMD_NETWORK_MODE) or {}).get("networkMode")
            self._mode_at = time.time()
        return self._mode

    def set_network_mode(self, mode):
        modes = self.net_modes
        if mode not in modes:
            raise ZltError("unknown network mode %r" % (mode,))
        self._netinfo = None
        self._mode = None
        return self.link.post(CMD_NETWORK_MODE, networkMode=mode, mode5g="0")

    def apn_settings(self):
        """Which access point the modem dials with, and what else is on file.

        Profiles this firmware ships with are read-only (``edit_flag`` 0); the
        one flagged ``default_flag`` is the one in use. There is no separate
        automatic/manual switch -- picking the profile named "Auto" is what
        hands the choice back to the network.
        """
        profiles = (self.link.get(CMD_APN_LIST) or {}).get("apn_list") or []
        globals_ = self.link.get(CMD_APN_GLOBAL, subcmd=3) or {}
        pdp = _PDP_BY_NAME.get(globals_.get("selectType"), 1)

        active = next((p for p in profiles if str(p.get("default_flag")) == "1"), {})
        manual = [_profile(p, pdp) for p in profiles if str(p.get("edit_flag")) == "1"]
        automatic = [_profile(p, pdp) for p in profiles
                     if str(p.get("edit_flag")) != "1"]
        return {
            "mode": "automatic" if not (active.get("apnName") or "") else "manual",
            "active": _profile(active, pdp) if active else {},
            "enabled_profile": active.get("name"),
            "manual": manual,
            "automatic": automatic,
            "mtu": globals_.get("apnMTU"),
        }

    def set_apn(self, apn, username="", password="", profile_id=None,
                pdp_type=1, auth_mode=0):
        """Point the modem at an access point, editing a profile if need be.

        The firmware takes the whole profile list back in one write, with the
        one to dial flagged. An existing entry for the same APN is reused, so
        repeated saves do not fill the (four-slot) editable list.
        """
        profiles = (self.link.get(CMD_APN_LIST) or {}).get("apn_list") or []
        wanted = apn.strip()

        target = None
        for profile in profiles:
            profile["default_flag"] = "0"
            if (profile.get("apnName") or "").strip().lower() == wanted.lower():
                target = profile

        if target is None:
            editable = [p for p in profiles if str(p.get("edit_flag")) == "1"]
            if len(editable) >= 4:
                raise ZltError(
                    "the unit holds at most four editable APN profiles and all "
                    "four are in use -- remove one on its own web page first")
            target = {"name": (profile_id or wanted)[:63], "edit_flag": "1"}
            profiles.append(target)

        target.update({
            "apnName": wanted,
            "apnUserName": username or "",
            "apnUserPassword": password or "",
            "selectAuthtication": str(int(auth_mode)),
            "default_flag": "1",
        })
        target.setdefault("name", wanted[:63])
        target.setdefault("edit_flag", "1")

        self.link.post(CMD_APN_LIST, apn_list=profiles)
        # The address family is a unit-wide setting rather than part of a
        # profile, so it is written separately -- and only when asked for.
        select = _PDP_BY_ID.get(int(pdp_type))
        if select:
            globals_ = self.link.get(CMD_APN_GLOBAL, subcmd=3) or {}
            if globals_.get("selectType") != select:
                self.link.post(CMD_APN_GLOBAL, subcmd=3, selectType=select,
                               apnNatName=globals_.get("apnNatName", "1"),
                               apnMTU=globals_.get("apnMTU", "1500"))
        self._netinfo = None
        return _profile(target, int(pdp_type))

    def lan_settings(self):
        lan = self.link.get(CMD_LAN) or {}
        return {
            "lan_ip": lan.get("lanIp", ""),
            "lan_netmask": lan.get("netMask", ""),
            "dhcp_enabled": str(lan.get("dhcpServer")) == "1",
            "dhcp_start": lan.get("ipBegin", ""),
            "dhcp_end": lan.get("ipEnd", ""),
            "dhcp_lease": lan.get("expireTime", ""),
            "dns": [d for d in (lan.get("main_dns"), lan.get("vice_dns")) if d],
        }

    def auto_reset(self):
        """When the unit rolls its own monthly counter over.

        Unlike the ZTE outdoor unit this cannot be switched off -- the counter
        always restarts on its start day -- so it reports as always enabled and
        ``set_auto_reset`` will only move the day.
        """
        traffic = self.link.get(CMD_TRAFFIC) or {}
        return {"enabled": True, "day": _int(traffic.get("startDate")) or 1,
                "fixed": True}

    def set_auto_reset(self, enabled, day=1):
        if not enabled:
            raise ZltError(
                "this unit always resets its monthly counter on the start day; "
                "that cannot be turned off, only moved to another day")
        traffic = self.link.get(CMD_TRAFFIC) or {}
        self.link.post(
            CMD_TRAFFIC,
            startDate=str(int(day)),
            limitSwitch=traffic.get("limitSwitch", "0"),
            limitSize=traffic.get("limitSize", ""),
            flow_limit_unit=traffic.get("flow_limit_unit", "1"),
            flow_sms_notice_sw=traffic.get("flow_sms_notice_sw", "0"),
            warn_percentage=traffic.get("warn_percentage", "0"),
            flow_notice_number=traffic.get("flow_notice_number", ""),
            flow_notice_text=traffic.get("flow_notice_text", ""),
        )
        return self.auto_reset()

    def counters_cleared_on(self):
        return (self.link.get(CMD_TRAFFIC) or {}).get("reset_traffic_lastTime") or None

    def qos_settings(self):
        """Empty on this firmware -- see ``CAPABILITIES``."""
        qos = self.link.get(CMD_QOS, subcmd=0) or {}
        return qos if qos.get("qosSw") else {}

    def set_qos(self, enable, priority=0):
        raise ZltError(
            "this unit has no QoS traffic prioritisation -- the network mode "
            "on its own is what the Optimise picker changes here")

    def at(self, command):
        out = self.link.post(CMD_AT, atInfo=_b64(command))
        return _unb64(out.get("flag", ""))

    def reboot(self):
        return self.link.post(CMD_REBOOT, rebootType=2)

    # -- messages --------------------------------------------------------

    def sms_list(self, limit=60, tags=None):
        """Stored messages, in the record shape ``core.sms`` normalises.

        The firmware hands the whole mailbox back as one comma-separated list of
        base64 blobs, each ``id flag sender YYYY/MM/DD HH:MM:SS body``. Its
        clock carries no zone, so the unit's own offset is appended to make the
        timestamp mean something off-device.
        """
        out = self.link.get(CMD_SMS_LIST, page_num=-1, subcmd=INBOX)
        offset = self.link.utc_offset()
        messages = []
        for blob in (out.get("sms_list") or "").split(","):
            record = _sms_record(blob, offset)
            if record:
                messages.append(record)
        messages.sort(key=lambda m: _int(m["id"]) or 0, reverse=True)
        return messages[:limit]

    def sms_mark_read(self, message_id):
        return self.link.post(CMD_SMS_LIST, index=str(message_id))

    def sms_delete(self, message_id):
        return self.link.post(CMD_SMS_DELETE, index=str(message_id), subcmd=INBOX)

    def sms_capacity(self):
        return self.link.get(CMD_SMS_CAPACITY)


# -- the indoor-router face -------------------------------------------------

class ZltRouter:
    """The LAN side: what is connected, over which radio, how strongly."""

    capabilities = CAPABILITIES

    BANDS = {
        "wlan24g_wifi_info": "2.4 GHz",
        "wlan5g_wifi_info": "5 GHz",
        "wlan6g_wifi_info": "6 GHz",
    }

    def __init__(self, link):
        self.link = link

    @property
    def password(self):
        return self.link.password

    @password.setter
    def password(self, value):
        self.link.password = value

    @property
    def logged_in(self):
        return self.link.logged_in()

    @logged_in.setter
    def logged_in(self, value):
        if not value:
            self.link.session_id = ""

    def login(self):
        return self.link.login()

    def devices(self):
        """Everything holding a lease, wireless and wired.

        The firmware has no per-client byte counters -- its ``flow`` field is
        always zero -- so totals are left as None and flagged, which is the same
        signal the ZTE client uses for a counter it cannot trust. The dashboard's
        own ledger is what fills the Devices tab either way.
        """
        raw = self.link.get(CMD_STATIONS)

        wireless = {}
        for field, band in self.BANDS.items():
            for entry in raw.get(field) or []:
                mac = (entry.get("mac") or "").lower()
                if mac:
                    wireless[mac] = (band, entry)

        devices = []
        for entry in raw.get("dhcp_list_info") or []:
            mac = (entry.get("mac") or "").lower()
            band, wifi = wireless.get(mac, (None, {}))
            if band is None:
                band = "WiFi" if entry.get("interface") == "wlan" else "LAN"
            name = (entry.get("hostname") or wifi.get("user") or "").strip()
            devices.append({
                "mac": mac,
                "hostname": name if name and name != "--" else "Unknown device",
                "ip": entry.get("ip", ""),
                "band": band,
                "rssi": _int(wifi.get("rssi")),
                "ssid": wifi.get("ssid", ""),
                "down_bytes": None,
                "up_bytes": None,
                "counters_ok": False,
                "down_speed": 0,
                "up_speed": 0,
            })
        devices.sort(key=lambda d: (d["band"] == "LAN", d["hostname"].lower()))
        return devices

    def wired_devices(self):
        raw = self.link.get(CMD_STATIONS)
        return [entry for entry in raw.get("dhcp_list_info") or []
                if entry.get("interface") != "wlan"]

    def wifi_info(self):
        raw = self.link.get(CMD_STATIONS)
        config = self.link.config(refresh=True)
        names = {}
        for field in self.BANDS:
            for entry in raw.get(field) or []:
                names.setdefault(field, entry.get("ssid", ""))
        return {
            "wifi_chip1_ssid1_ssid": names.get("wlan24g_wifi_info", ""),
            "wifi_chip2_ssid1_ssid": names.get("wlan5g_wifi_info", ""),
            "cr_version": config.get("fake_version", ""),
            "wa_inner_version": config.get("real_fwversion", ""),
        }

    def status(self):
        loop = self.link.get(CMD_LOOP)
        return {
            "modem_main_state": loop.get("networkState", ""),
            "signalbar": loop.get("signal_lvl", ""),
            "network_type": loop.get("network_type_str", ""),
            "network_provider": loop.get("network_operator", ""),
        }

    def reboot(self):
        """One box, one restart -- same call the outdoor face makes."""
        return self.link.post(CMD_REBOOT, rebootType=2)

    def post(self, fields):
        """Kept for callers written against the ZTE goform client."""
        if fields.get("goformId") == "REBOOT_DEVICE":
            return self.reboot()
        raise ZltError("this device has no goform interface (%r)" % (fields,))


# -- helpers ----------------------------------------------------------------

def modes_for(bitmask):
    """The radio modes a unit offers, from the bitmask it advertises.

    ``networkMode_opt`` is a hex string read four bits at a time, least
    significant nibble last -- the same expansion the stock web app does before
    deciding which entries of its own mode list to show.
    """
    bits = _bits(bitmask)
    modes = {}
    for index, mode in sorted(MODE_BITS.items()):
        if index < len(bits) and bits[index] == "1" and mode in MODE_LABELS:
            modes[mode] = MODE_LABELS[mode]
    # A unit that advertises nothing still has to be steerable, so fall back to
    # the three every one of these modems has.
    return modes or {mode: MODE_LABELS[mode] for mode in ("4", "C", "E")}


def goals_for(modes):
    """Best available mode for each of the dashboard's Optimise goals."""
    goals = {}
    for goal, preferences in MODE_GOALS.items():
        for mode in preferences:
            if mode in modes:
                goals[goal] = mode
                break
    return goals


def _bits(bitmask):
    text = "".join(
        bin(int(char, 16))[2:].zfill(4) for char in str(bitmask or "")
        if char in "0123456789abcdefABCDEF"
    )
    return text[::-1]


def _carrier(index, info, upper, lower):
    """One carrier from the ``*_5G``/plain pairs of fields netinfo reports."""
    carrier = {"index": index, "primary": index == 0}
    for name, field in (("rsrp", "RSRP"), ("rsrq", "RSRQ"), ("sinr", "SINR"),
                        ("rssi", "RSSI"), ("pci", "PCI")):
        carrier[name] = _float(info.get(field + upper))
    carrier["earfcn"] = _float(info.get("FREQ" + upper))
    carrier["band"] = _float(info.get("currentband" + lower))
    carrier["bandwidth"] = _float(info.get("bandwidth" + lower))
    return carrier


def _profile(profile, pdp_type):
    """A firmware APN record in the shape the dashboard renders."""
    return {
        "profilename": profile.get("name", ""),
        "wanapn": profile.get("apnName", ""),
        "username": profile.get("apnUserName", ""),
        "password": profile.get("apnUserPassword", ""),
        "pdpType": pdp_type,
        "pppAuthMode": _int(profile.get("selectAuthtication")) or 0,
        "profileId": profile.get("name", ""),
        "editable": str(profile.get("edit_flag")) == "1",
    }


_PDP_BY_ID = {1: "IP", 2: "IPV6", 3: "IPV4V6"}
_PDP_BY_NAME = {"IP": 1, "IPV6": 2, "IPV4V6": 3}


def _sms_record(blob, offset):
    """One base64 mailbox entry -> the record shape ``core.sms`` expects."""
    text = _unb64(blob)
    parts = text.split(" ")
    if len(parts) < 6:
        return None
    index, flag, sender, date, clock = parts[:5]
    try:
        year, month, day = (int(p) for p in date.split("/"))
        hour, minute, second = (int(p) for p in clock.split(":"))
    except ValueError:
        return None
    return {
        "id": index,
        "number": sender,
        "content": " ".join(parts[5:]),
        # core.sms wants two-digit years and the offset in quarter hours.
        "date": "%02d,%02d,%02d,%02d,%02d,%02d,%+d"
                % (year % 100, month, day, hour, minute, second, offset // 900),
        # This firmware marks a message read with 1; core.sms reads 1 as unread,
        # so the sense is flipped here rather than special-cased there.
        "tag": "0" if flag == "1" else "1",
    }


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode()


def _unb64(blob):
    """Decode a mailbox blob, whichever way this build encoded it.

    Bodies are UTF-8 on most builds and raw 8-bit on others (the X17U among
    them), so a failed decode falls back rather than losing the message.
    """
    blob = (blob or "").strip()
    if not blob:
        return ""
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4))
    except (ValueError, TypeError):
        return blob
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _tech(raw):
    text = str(raw or "")
    for pattern, token in TECH_NAMES:
        if pattern.search(text):
            return token
    return text


def _band_label(info):
    """"B7 + n78", the way a person would say which bands are carrying this."""
    parts = []
    lte = _band_name(info.get("currentband"))
    nr = _band_name(info.get("currentband_5g"), "n")
    if lte:
        parts.append(lte)
    if nr:
        parts.append(nr)
    return " + ".join(parts)


def _band_name(value, prefix="B"):
    number = _int(value)
    return "%s%d" % (prefix, number) if number else ""


def _split(raw):
    return [part for part in str(raw or "").split(",") if part.strip()]


def _mib(value, floor=0):
    """A megabyte figure as bytes, never smaller than a counter we trust more."""
    number = _float(value)
    if number is None:
        return floor
    return max(floor, int(number * MIB))


def _int(value, base=10):
    try:
        return int(str(value).strip(), base)
    except (TypeError, ValueError):
        return None


def _float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _random_hex():
    return os.urandom(32).hex()


def _login_failure(out):
    """Turn the firmware's login refusal into something worth reading."""
    if str(out.get("message")) == "alreadyLogin":
        return ("someone else is already signed in to the device -- log out of "
                "its own web page, or wait for that session to expire")
    if str(out.get("login_fail2")) == "fail":
        wait = out.get("login_time") or "a few"
        return ("too many failed logins; the device is locked for another %s "
                "seconds" % wait)
    if str(out.get("login_fail")) == "fail":
        return ("device login rejected -- check the password (%s attempt%s so "
                "far; it locks out after a few)"
                % (out.get("login_times", "?"),
                   "" if str(out.get("login_times")) == "1" else "s"))
    return "device login rejected -- check the password in config.json"


def probe(host, timeout=3):
    """Is there a ZLT-style web interface at this address?

    Cheap and unauthenticated: the login-token endpoint answers before any
    session exists, and nothing else this app talks to responds to it.
    """
    session = ZltSession(host, timeout=timeout)
    try:
        out = session.rpc(CMD_LOGIN_TOKEN)
    except (OSError, ZltError, urllib.error.URLError):
        return False
    return bool(out.get("success") and out.get("token"))
