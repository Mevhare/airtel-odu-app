"""Client for the indoor CPE at 192.168.18.1 (ZTE MF296A, Airtel firmware).

Everything goes through the ``goform`` interface. Two quirks matter:

  * ``Referer`` must be set or the firmware returns empty values for everything
  * commands that return a *list* (``station_list``, ``lan_station_list``) only
    work when ``multi_data=1`` is left OFF. Adding it makes the firmware answer
    with an empty string instead of the array, which is why the connected-device
    list looks unsupported until you drop the parameter.

Scalar commands are the opposite: they want ``multi_data=1`` so several can be
fetched in one round trip.
"""

import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request

from .errors import DeviceError
from .odu import CAPABILITIES

# The per-device byte counters are signed 32-bit. Two things happen at the top
# of that range and they have to be told apart:
#
#   * an ordinary wrap into negative territory, which folds back cleanly;
#   * saturation, where the firmware pins the value at INT32_MIN or INT32_MAX
#     and leaves it there. Observed on a client that had moved a few GB: it
#     reported tx_total=-2147483648 and rx_total=2147483647 at the same time.
#
# A pinned counter is not a number, it is "gave up counting", so it is reported
# as None rather than folded into a plausible-looking 2 GB.
UINT32 = 1 << 32
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


class RouterError(DeviceError):
    pass


class Router:
    capabilities = CAPABILITIES

    def __init__(self, host, password="admin", timeout=10):
        self.host = host
        self._password = password
        self._rejected_password = None
        self.timeout = timeout
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor()
        )
        self.logged_in = False
        self._locked_until = 0.0
        self._lock = threading.RLock()

    @property
    def password(self):
        return self._password

    @password.setter
    def password(self, value):
        if value != self._password:
            self._rejected_password = None
        self._password = value

    # -- transport ---------------------------------------------------------

    def _headers(self):
        return {
            "Referer": "http://%s/index.html" % self.host,
            "Origin": "http://%s" % self.host,
            "X-Requested-With": "XMLHttpRequest",
        }

    def get(self, commands, multi=None):
        """Fetch one or more goform values.

        ``multi`` defaults to True for several commands and False for a single
        one, which matches the firmware's expectations described above.
        """
        if isinstance(commands, (list, tuple)):
            joined = ",".join(commands)
            if multi is None:
                multi = len(commands) > 1
        else:
            joined = commands
            if multi is None:
                multi = False

        params = {"isTest": "false", "cmd": joined}
        if multi:
            params["multi_data"] = "1"

        url = "http://%s/goform/goform_get_cmd_process?%s" % (
            self.host, urllib.parse.urlencode(params)
        )
        request = urllib.request.Request(url, headers=self._headers())
        with self.opener.open(request, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            raise RouterError("unparseable response for %r" % joined)

    def post(self, fields):
        url = "http://%s/goform/goform_set_cmd_process" % self.host
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(url, data=data, headers=self._headers())
        with self.opener.open(request, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}

    # -- auth --------------------------------------------------------------

    def login(self):
        """Log in, trying the hash schemes this firmware family is known to use.

        Which one applies varies by build, so each candidate is attempted and the
        first that reports success wins. The session then lives in the cookie jar.
        """
        with self._lock:
            waiting = self._locked_until - time.time()
            if waiting > 0:
                raise RouterError(
                    "router is rate-limiting logins for another %d seconds"
                    % round(waiting)
                )

            info = self.get(["LD", "login_lock_time", "psw_fail_num_str"]) or {}
            salt = info.get("LD", "") or ""
            locked = info.get("login_lock_time")
            try:
                if locked and int(locked) > 0:
                    self._locked_until = time.time() + int(locked)
                    raise RouterError(
                        "router is rate-limiting logins for another %s seconds"
                        % locked
                    )
            except (TypeError, ValueError):
                pass

            upper_sha = lambda text: hashlib.sha256(text.encode()).hexdigest().upper()

            if salt:
                # When LD is served, this firmware uses SHA256(SHA256(password)+LD)
                # exclusively -- do not cycle through fallback candidates on a single
                # login attempt and burn multiple lockout slots.
                candidates = [upper_sha(upper_sha(self.password) + salt.upper())]
            else:
                candidates = [
                    upper_sha(self.password),
                    urllib.parse.quote(self.password.encode("utf-8")),
                ]

            for candidate in candidates:
                out = self.post({
                    "isTest": "false",
                    "goformId": "LOGIN",
                    "password": candidate,
                })
                result = str((out or {}).get("result", ""))
                if result == "2":
                    raise RouterError(
                        "Someone else is already signed in to the indoor router at "
                        "%s — log out of its own web page first, or wait for that "
                        "session to expire." % self.host
                    )
                # The result code is not always a reliable signal on this firmware,
                # so confirm by reading a value that only answers when authenticated.
                if result in ("0", "4") or self._authenticated():
                    self.logged_in = True
                    self._rejected_password = None
                    return True

            self._rejected_password = self.password
            after = self.get(["login_lock_time", "psw_fail_num_str"]) or {}
            after_locked = after.get("login_lock_time")
            try:
                if after_locked and int(after_locked) > 0:
                    self._locked_until = time.time() + int(after_locked)
                    raise RouterError(
                        "Too many failed logins; router is rate-limiting logins "
                        "for another %s seconds." % after_locked
                    )
            except (TypeError, ValueError):
                pass
            remaining = after.get("psw_fail_num_str")
            if remaining is not None and str(remaining) != "":
                raise RouterError(
                    "router login rejected — check the indoor router password "
                    "(%s attempt%s left before lockout)."
                    % (remaining, "" if str(remaining) == "1" else "s")
                )
            raise RouterError(
                "router login rejected -- check the password in config.json"
            )

    def _authenticated(self):
        """Probe a command that is genuinely session-gated.

        Most scalars (``cr_version``, ``psw_fail_num_str``) answer to anonymous
        callers, so they cannot be used to detect a lapsed session. ``station_list``
        can: it returns an array once authenticated and an empty *string* before.
        """
        try:
            return isinstance(self.get("station_list").get("station_list"), list)
        except (RouterError, OSError):
            return False

    def ensure_login(self):
        """Log in if the session has lapsed.

        There is no session endpoint, so this probes a command that stays blank
        until authenticated.
        """
        with self._lock:
            if self._authenticated():
                self.logged_in = True
                return
            if (self._rejected_password is not None
                    and self.password == self._rejected_password):
                raise RouterError(
                    "router login rejected — sign in on the dashboard with the "
                    "indoor router password."
                )
            self.login()

    # -- telemetry ---------------------------------------------------------

    def devices(self):
        """Connected WiFi clients, each with cumulative bytes and live speed.

        Note the direction of the counters is from the *router's* point of view:
        ``tx_*`` is router-to-device, i.e. what the device downloaded.
        """
        with self._lock:
            raw = self.get("station_list").get("station_list")
            if not isinstance(raw, list):
                # Session has lapsed (the firmware answers with an empty string).
                if (self._rejected_password is not None
                        and self.password == self._rejected_password):
                    raise RouterError(
                        "router login rejected — sign in on the dashboard with "
                        "the indoor router password."
                    )
                self.login()
                raw = self.get("station_list").get("station_list")
            if not isinstance(raw, list):
                return []

        devices = []
        for entry in raw:
            name = (entry.get("hostname") or "").strip()
            down = _unsigned(entry.get("tx_total"))
            up = _unsigned(entry.get("rx_total"))
            devices.append({
                "mac": entry.get("mac_addr", ""),
                "hostname": name if name and name != "--" else "Unknown device",
                "ip": entry.get("ip_addr", ""),
                "band": entry.get("ssid_index", ""),
                "rssi": _to_int(entry.get("rssi")),
                "down_bytes": down,
                "up_bytes": up,
                # False once the firmware has stopped counting for this client;
                # its totals are then meaningless and must not feed the ledger.
                "counters_ok": down is not None and up is not None,
                "down_speed": _rate(entry.get("tx_speed")),
                "up_speed": _rate(entry.get("rx_speed")),
            })
        devices.sort(key=lambda d: (d["down_bytes"] or 0) + (d["up_bytes"] or 0),
                     reverse=True)
        return devices

    def wired_devices(self):
        self.ensure_login()
        raw = self.get("lan_station_list").get("lan_station_list") or []
        return raw if isinstance(raw, list) else []

    def wifi_info(self):
        self.ensure_login()
        return self.get([
            "wifi_chip1_ssid1_ssid", "wifi_chip2_ssid1_ssid",
            "cr_version", "wa_inner_version", "Language",
        ])

    def status(self):
        self.ensure_login()
        return self.get([
            "modem_main_state", "signalbar", "network_type", "network_provider",
            "opms_wan_mode", "opms_wan_auto_mode", "ppp_status",
        ])

    def reboot(self):
        return self.post({"goformId": "REBOOT_DEVICE"})


def _to_int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _unsigned(value):
    """Fold a wrapped 32-bit counter back into unsigned range.

    Returns None for a counter the firmware has pinned at either int32 limit --
    see the note at the top of this module.
    """
    number = _to_int(value)
    if number is None:
        return 0
    if number in (INT32_MIN, INT32_MAX):
        return None
    return number + UINT32 if number < 0 else number


def _rate(value):
    """Speeds are instantaneous and never saturate, so None means zero here."""
    return _unsigned(value) or 0
