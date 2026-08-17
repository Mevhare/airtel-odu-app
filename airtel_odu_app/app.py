"""Local dashboard for the Airtel ODU + indoor CPE.

Serves a small JSON API and the static dashboard on the LAN, so any phone on the
same WiFi can open it. Nothing leaves the network and there is no account.

Run with:  airtel-odu-app
"""

import datetime
import http.cookies
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import db, sms
from .core.collector import Collector
from .core.errors import DeviceError
from .core.odu import QOS_PRIORITIES, optimise_mode_from_qos

SESSION_COOKIE = "wifiapp_session"
# Reachable without a session, so the SPA can load and show its own login form.
PUBLIC_API = {"/api/session", "/api/login"}

# Where the code (and its bundled web assets / example config) lives -- fixed,
# wherever the package was installed.
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(PKG_DIR, "web")

# Where this install's own data lives -- the directory the app is run from, so
# a pip-installed console script keeps its config/db next to wherever the user
# chose to run it, not buried in site-packages. Override with the env var if
# you want to run multiple instances or keep data elsewhere.
DATA_DIR = os.environ.get("AIRTEL_ODU_APP_DATA_DIR") or os.getcwd()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

# AT commands are a genuine escape hatch, so the console only accepts query
# forms. Anything that assigns a value is refused before it reaches the modem.
SAFE_AT = re.compile(r"^AT[A-Z0-9+^*$!%&/#_\[\]\.-]*\??$", re.IGNORECASE)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        # First run in this directory: seed it from the bundled template
        # rather than crashing, so `airtel-odu-app` works right after install.
        shutil.copyfile(os.path.join(PKG_DIR, "config.example.json"), CONFIG_PATH)
        print("No config.json in %s -- created one from the template." % DATA_DIR)
        print("Edit it with your ODU/router host and password, then restart.")
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wifiapp"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        pass  # the collector's own output is the interesting stream

    def handle_one_request(self):
        # A browser closing a tab mid-poll resets the socket. That is normal and
        # not worth a traceback, so it dies quietly and the thread ends.
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _query(self):
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        path, query = self._query()
        try:
            if path.startswith("/api/"):
                if path not in PUBLIC_API and not self._authenticated():
                    return self._send(401, {"error": "not logged in"})
                return self._api_get(path, query)
            return self._static(path)
        except DeviceError as exc:
            # The hardware refused or is not answering -- that is a bad gateway,
            # not a bug in here, and the message is worth showing as-is.
            return self._send(502, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})

    def do_POST(self):
        path, _ = self._query()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "body must be JSON"})
        try:
            if path not in PUBLIC_API and not self._authenticated():
                return self._send(401, {"error": "not logged in"})
            return self._api_post(path, payload)
        except DeviceError as exc:
            return self._send(502, {"error": str(exc)})
        except Exception as exc:
            return self._send(500, {"error": str(exc)})

    def _authenticated(self):
        token = (self.server.config.get("auth") or {}).get("session_token")
        if not token:
            return False
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return False
        cookie = jar.get(SESSION_COOKIE)
        return bool(cookie) and secrets.compare_digest(cookie.value, token)

    def _static(self, path):
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.normpath(os.path.join(WEB_DIR, relative))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        extension = os.path.splitext(target)[1]
        with open(target, "rb") as handle:
            body = handle.read()
        return self._send(200, body, CONTENT_TYPES.get(extension, "application/octet-stream"))

    # -- API ---------------------------------------------------------------

    def _api_get(self, path, query):
        collector = self.server.collector
        config = self.server.config
        hours = int((query.get("hours") or ["24"])[0])

        if path == "/api/session":
            # The device description rides along unauthenticated: the login
            # screen needs it to know whether to ask for one password or two,
            # and it says nothing a stranger on the LAN could not see anyway.
            return self._send(200, {
                "authenticated": self._authenticated(),
                "device": collector.device_kind,
                "capabilities": dict(collector.odu.capabilities),
            })

        if path == "/api/overview":
            snapshot = collector.snapshot()
            snapshot["cap"] = config["alerts"].get("data_cap_bytes")
            snapshot["uptime_24h"] = db.uptime_ratio(24)
            snapshot["writes_enabled"] = config["safety"]["allow_writes"]
            snapshot["projection"] = self._projection(snapshot)
            snapshot.update(self._device_info(collector))
            snapshot["billing_day"] = config["alerts"].get("billing_day", 1)
            snapshot["optimise_mode"] = self._optimise_mode(collector, snapshot)
            return self._send(200, snapshot)

        if path == "/api/lan-url":
            return self._send(200, {"url": _lan_url(config["server"]["port"])})

        if path == "/api/devices":
            snap = collector.snapshot().get("devices") or {"items": []}
            # The router's own per-client counters (down_bytes/up_bytes below)
            # reset whenever a device disassociates and reconnects. tracked_bytes
            # comes from our own accumulated ledger instead, which re-baselines
            # on a reset rather than losing the running total. Which window of
            # that ledger to sum is caller-chosen, same presets as the Data tab.
            tracked_since = db.tracked_since()
            try:
                since, until, _, range_label = _resolve_range(query, config)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            tracked = {row["mac"]: row["down"] + row["up"]
                       for row in db.traffic_by_device(since, until)}
            devices = dict(snap, tracked_since=tracked_since,
                           range=range_label, range_since=since, range_until=until,
                           items=[dict(d, tracked_bytes=tracked.get(d["mac"]))
                                  for d in (snap.get("items") or [])])
            # Loopback means the dashboard itself is running on this PC, so
            # its own LAN-facing IP -- the one the router actually sees -- is
            # what identifies its row in the device list, not 127.0.0.1.
            client_ip = self.client_address[0]
            devices["client_ip"] = _lan_ip() if client_ip in ("127.0.0.1", "::1") else client_ip
            return self._send(200, devices)

        if path == "/api/live":
            return self._send(200, collector.live())

        if path == "/api/live/history":
            return self._send(200, collector.live_history())

        if path == "/api/network-config":
            # Read-only: what the modem is dialling with.
            try:
                return self._send(200, {
                    "apn": collector.odu.apn_settings(),
                    "lan": collector.odu.lan_settings(),
                })
            except (DeviceError, OSError) as exc:
                return self._send(502, {"error": str(exc)})

        if path == "/api/qos":
            try:
                return self._send(200, collector.odu.qos_settings())
            except DeviceError as exc:
                return self._send(502, {"error": str(exc)})

        if path == "/api/history/signal":
            return self._send(200, db.history("signal_samples", hours))

        if path == "/api/history/usage":
            return self._send(200, db.history("usage_samples", hours))

        if path == "/api/usage/series":
            source = (query.get("source") or [None])[0]
            group = (query.get("group") or [None])[0]
            if group not in ALLOWED_BUCKETS:
                group = None
            try:
                since, until, default_bucket, label = _resolve_range(
                    query, self.server.config, source)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, self._series(
                since, until, group or default_bucket, source, label))

        if path == "/api/usage/devices":
            window = (query.get("range") or ["day"])[0]
            since, _ = _window(window, self.server.config)
            items = (collector.snapshot().get("devices") or {}).get("items", [])
            names = {d["mac"]: d["hostname"] for d in items}
            # Same loopback-aware resolution as /api/devices, so whichever
            # row's mac matches this browser's own IP gets tagged as "you".
            client_ip = self.client_address[0]
            if client_ip in ("127.0.0.1", "::1"):
                client_ip = _lan_ip()
            client_mac = next((d["mac"] for d in items if d.get("ip") == client_ip), None)
            rows = db.traffic_by_device(since)
            for row in rows:
                row["hostname"] = names.get(row["mac"]) or row["mac"]
                row["you"] = row["mac"] == client_mac
            return self._send(200, rows)

        if path == "/api/events":
            return self._send(200, db.recent_events(200))

        if path == "/api/sms":
            messages = collector.refresh_sms() if query.get("refresh") \
                else collector.messages()
            return self._send(200, {
                "messages": messages,
                "msisdn": db.carrier_msisdn(),
                "number": sms.format_number(db.carrier_msisdn()),
                "unread": sum(1 for m in messages if m["unread"]),
            })

        if path == "/api/usage/carrier":
            window = (query.get("range") or ["cycle"])[0]
            since, _ = _window(window, config, "carrier")
            return self._send(200, db.carrier_days(since))

        if path.startswith("/api/device/"):
            mac = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            return self._send(200, db.device_history(mac, hours))

        if path == "/api/diagnostics":
            return self._send(200, self._diagnostics())

        if path == "/api/speedtest":
            megabytes = min(50, max(1, int((query.get("mb") or ["10"])[0])))
            try:
                return self._send(200, _speedtest(megabytes))
            except OSError as exc:
                return self._send(502, {"error": str(exc)})

        if path == "/api/at":
            if not collector.odu.capabilities.get("at"):
                return self._send(501, {
                    "error": "this device does not expose an AT passthrough to "
                             "the account the dashboard signs in with"})
            command = (query.get("cmd") or [""])[0].strip()
            if not SAFE_AT.match(command):
                return self._send(400, {
                    "error": "only read-only AT queries are accepted "
                             "(no '=' assignments)"
                })
            try:
                return self._send(200, {"cmd": command,
                                        "result": collector.odu.at(command)})
            except DeviceError as exc:
                return self._send(502, {"error": str(exc)})

        return self._send(404, {"error": "no such endpoint"})

    def _api_post(self, path, payload):
        collector = self.server.collector
        config = self.server.config

        if path == "/api/login":
            return self._handle_login(collector, config, payload)

        if path == "/api/logout":
            return self._handle_logout(config)

        if not config["safety"]["allow_writes"]:
            return self._send(403, {
                "error": "writes are disabled. Set safety.allow_writes to true "
                         "in config.json to enable them."
            })

        if path == "/api/network-mode":
            mode = payload.get("mode")
            if mode not in collector.odu.net_modes:
                return self._send(400, {"error": "unrecognised mode %r" % (mode,)})
            previous = collector.snapshot()["odu"]["netinfo"].get("net_select")
            collector.odu.set_network_mode(mode)
            db.log_event("mode_change", "switched from %s to %s" % (previous, mode))
            self._arm_revert(collector, previous,
                             config["safety"]["band_switch_revert_seconds"])
            return self._send(200, {"ok": True, "mode": mode, "previous": previous})

        if path == "/api/qos":
            if not collector.odu.capabilities.get("qos"):
                return self._send(501, {
                    "error": "this device has no QoS traffic prioritisation"})
            enable = bool(payload.get("enable"))
            priority = payload.get("priority", 0)
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                return self._send(400, {"error": "priority must be a number"})
            if priority not in QOS_PRIORITIES:
                return self._send(400, {"error": "unrecognised priority %r" % (priority,)})
            try:
                collector.odu.set_qos(enable, priority)
            except DeviceError as exc:
                return self._send(502, {"error": str(exc)})
            collector.note_qos_change(enable, priority)
            db.log_event("qos_change", "QoS %s (priority %d)"
                         % ("on" if enable else "off", priority))
            return self._send(200, {"ok": True, "enable": enable, "priority": priority})

        if path == "/api/reboot":
            target = payload.get("target")
            if target not in ("odu", "router", "both"):
                return self._send(400,
                                  {"error": "target must be 'odu', 'router' or 'both'"})
            if collector.odu.capabilities.get("single_device"):
                # One box wearing both hats, so every target means the same
                # restart -- and doing it twice would only cut the second one
                # short.
                collector.odu.reboot()
            else:
                # The router first: it comes back faster, so by the time the
                # outdoor unit has finished booting there is already a LAN.
                if target in ("router", "both"):
                    collector.router.reboot()
                if target in ("odu", "both"):
                    collector.odu.reboot()
            db.log_event("reboot", "%s reboot requested from the dashboard" % target)
            return self._send(200, {"ok": True, "target": target})

        if path == "/api/sms/read":
            for message_id in payload.get("ids") or []:
                collector.odu.sms_mark_read(message_id)
            return self._send(200, {"ok": True})

        if path == "/api/sms/delete":
            message_id = payload.get("id")
            if message_id is None:
                return self._send(400, {"error": "id is required"})
            collector.odu.sms_delete(message_id)
            return self._send(200, {"ok": True, "messages": collector.refresh_sms()})

        if path == "/api/apn":
            apn = (payload.get("apn") or "").strip()
            if not apn:
                return self._send(400, {"error": "APN is required"})
            previous = collector.odu.apn_settings().get("active")
            try:
                collector.odu.set_apn(
                    apn, payload.get("username", ""), payload.get("password", ""),
                    profile_id=previous.get("profileId") if previous else None,
                    pdp_type=payload.get("pdp_type", 1),
                    auth_mode=payload.get("auth_mode", 0))
            except DeviceError as exc:
                return self._send(502, {"error": str(exc)})
            db.log_event("apn_change", "APN changed to %s" % apn)
            self._arm_apn_revert(collector, previous,
                                 config["safety"]["band_switch_revert_seconds"])
            return self._send(200, {"ok": True, "apn": apn})

        if path == "/api/auto-reset":
            enabled = bool(payload.get("enabled"))
            day = int(payload.get("day") or config["alerts"].get("billing_day", 1))
            collector.odu.set_auto_reset(enabled, day)
            db.log_event("auto_reset", "monthly counter reset %s (day %d)"
                         % ("enabled" if enabled else "disabled", day))
            return self._send(200, collector.odu.auto_reset())

        if path == "/api/billing-day":
            try:
                day = int(payload.get("day"))
            except (TypeError, ValueError):
                return self._send(400, {"error": "pick a day between 1 and 28"})
            if not 1 <= day <= 28:
                return self._send(400, {"error": "pick a day between 1 and 28"})
            config["alerts"]["billing_day"] = day
            save_config(config)
            db.log_event("billing_day", "billing cycle start set to day %d" % day)
            return self._send(200, {"ok": True, "day": day})

        return self._send(404, {"error": "no such endpoint"})

    def _handle_login(self, collector, config, payload):
        """Verify the real device passwords, then remember the browser.

        Logs in against the same Odu/Router instances the collector's own
        background threads use -- never a second, competing client -- so this
        never risks tripping the ODU's login rate limiter on its own account.
        """
        odu_password = (payload.get("odu_password") or "").strip()
        # A unit that is its own router has one password and one session, so
        # the second field is neither asked for nor used.
        single = collector.odu.capabilities.get("single_device")
        router_password = odu_password if single \
            else (payload.get("router_password") or "").strip()
        if not odu_password or not router_password:
            return self._send(400, {
                "error": "a password is required" if single
                         else "both passwords are required"})

        previous_odu = collector.odu.password
        collector.odu.password = odu_password
        collector.odu.session = None
        try:
            collector.odu.login()
        except DeviceError as exc:
            collector.odu.password = previous_odu
            collector.odu.session = None
            return self._send(401, {"error": str(exc), "field": "odu"})

        if not single:
            previous_router = collector.router.password
            collector.router.password = router_password
            collector.router.logged_in = False
            try:
                collector.router.login()
            except DeviceError as exc:
                collector.router.password = previous_router
                collector.router.logged_in = False
                return self._send(401, {"error": str(exc), "field": "router"})

        config["odu"]["password"] = odu_password
        config["router"]["password"] = router_password
        token = (config.get("auth") or {}).get("session_token") or secrets.token_hex(32)
        config["auth"] = {"session_token": token}
        save_config(config)
        db.log_event("login", "Signed in to the dashboard")

        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        cookie = http.cookies.SimpleCookie()
        cookie[SESSION_COOKIE] = token
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["max-age"] = 400 * 86400
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Lax"
        self.send_header("Set-Cookie", cookie[SESSION_COOKIE].OutputString())
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_logout(self, config):
        """Drop the shared session token, so every browser holding the old
        cookie is signed out -- there is only ever the one session, per the
        single-household login model above."""
        config["auth"] = {"session_token": None}
        save_config(config)
        db.log_event("logout", "Signed out of the dashboard")

        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        cookie = http.cookies.SimpleCookie()
        cookie[SESSION_COOKIE] = ""
        cookie[SESSION_COOKIE]["path"] = "/"
        cookie[SESSION_COOKIE]["max-age"] = 0
        cookie[SESSION_COOKIE]["httponly"] = True
        cookie[SESSION_COOKIE]["samesite"] = "Lax"
        self.send_header("Set-Cookie", cookie[SESSION_COOKIE].OutputString())
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- helpers -----------------------------------------------------------

    def _device_info(self, collector):
        """What is on the other end, and what it can be asked to do.

        The dashboard is built for two families of hardware that differ in what
        they expose (see ``core/hardware.py``), so the controls it draws follow
        this rather than assuming the ZTE pair.
        """
        return {
            "device": collector.device_kind,
            "capabilities": dict(collector.odu.capabilities),
            "modes": collector.odu.net_modes,
            "mode_goals": collector.odu.mode_goals,
        }

    def _optimise_mode(self, collector, snapshot):
        """Which Optimise goal the hardware currently reflects.

        On the ZTE pair the QoS priority is the tell, since the radio mode alone
        does not distinguish Game from Performance. Hardware without QoS has
        only the radio to go on, so the goal is read back from the mode itself --
        which is the whole of what Optimise changes there.
        """
        odu = snapshot.get("odu") or {}
        if collector.odu.capabilities.get("qos"):
            return optimise_mode_from_qos((odu.get("settings") or {}).get("qos"))

        current = (odu.get("netinfo") or {}).get("net_select")
        goals = collector.odu.mode_goals
        for goal in ("game", "performance", "default"):
            if current and goals.get(goal) == current:
                return goal
        return None

    def _arm_revert(self, collector, previous, seconds):
        """Undo a mode change if the link has not come back in time."""
        def revert():
            time.sleep(seconds)
            snapshot = collector.snapshot()
            if snapshot["odu"] and not snapshot["odu"]["connected"]:
                try:
                    collector.odu.set_network_mode(previous)
                    db.log_event("mode_revert",
                                 "no link after %ds, reverted to %s" % (seconds, previous))
                except DeviceError as exc:
                    db.log_event("mode_revert_failed", str(exc))

        threading.Thread(target=revert, daemon=True).start()

    def _arm_apn_revert(self, collector, previous, seconds):
        """Undo an APN edit if the link has not come back in time."""
        if not previous:
            return

        def revert():
            time.sleep(seconds)
            snapshot = collector.snapshot()
            if snapshot["odu"] and not snapshot["odu"]["connected"]:
                try:
                    collector.odu.set_apn(
                        previous.get("wanapn", ""), previous.get("username", ""),
                        previous.get("password", ""),
                        profile_id=previous.get("profileId"),
                        pdp_type=previous.get("pdpType", 1),
                        auth_mode=previous.get("pppAuthMode", 0))
                    db.log_event("apn_revert", "no link after %ds, reverted APN" % seconds)
                except DeviceError as exc:
                    db.log_event("apn_revert_failed", str(exc))

        threading.Thread(target=revert, daemon=True).start()

    def _series(self, since, until, bucket, source=None, label="custom"):
        """Usage bucketed at the requested granularity for [since, until]."""
        if source == "carrier":
            # Airtel's own daily totals. No up/down split -- it quotes one
            # figure -- so it all goes in the down column. The feed is daily
            # at the finest, so hour/5min group requests are clamped to a day.
            effective_bucket = "day" if bucket in ("5min", "hour") else bucket
            days = db.carrier_days(since, until)
            buckets = {}
            for d in days:
                key = _bucket_start(d["ts"], effective_bucket)
                entry = buckets.setdefault(key, {"ts": key, "down": 0, "up": 0})
                entry["down"] += d["bytes"] or 0
            points = [buckets[key] for key in sorted(buckets)]

            # The equal-length window immediately before this one, for a
            # "vs previous period" comparison -- None (not 0) when Airtel's
            # texts don't go back that far, so a missing prior period reads
            # as "nothing to compare" rather than a fake jump from zero.
            prev_since = since - (until - since)
            prev_days = db.carrier_days(prev_since, since)
            prev_total = sum(d["bytes"] or 0 for d in prev_days) if prev_days else None

            return {"range": label, "bucket": effective_bucket, "since": since, "until": until,
                    "tracked_since": days[0]["ts"] if days else None,
                    "points": points,
                    "total": sum(p["down"] for p in points),
                    "prev_total": prev_total}

        if bucket == "5min":
            # The ledger only keeps hourly buckets, which is far too coarse for
            # a fine-grained view, so this one is derived from the raw samples
            # (which are only retained for a short window -- older custom
            # ranges at this granularity will simply come back empty).
            now = int(time.time())
            hours_ago = max(1, -(-(now - since) // 3600))
            rows = _sample_deltas(db.history("usage_samples", hours_ago,
                                             "ts, total_down, total_up"))
            rows = [r for r in rows if since <= r["hour"] <= until]
        else:
            rows = db.traffic_rows(since, until, source=source)

        buckets = {}
        for row in rows:
            key = _bucket_start(row["hour"], bucket)
            entry = buckets.setdefault(key, {"ts": key, "down": 0, "up": 0})
            entry["down"] += row["down"] or 0
            entry["up"] += row["up"] or 0

        points = [buckets[key] for key in sorted(buckets)]
        return {"range": label, "bucket": bucket, "since": since, "until": until,
                "tracked_since": db.tracked_since(), "points": points,
                "total": sum(p["down"] + p["up"] for p in points)}

    def _projection(self, snapshot):
        """Cycle-to-date usage, plus a burn-rate estimate when a cap is set."""
        cap = self.server.config["alerts"].get("data_cap_bytes")
        odu = snapshot.get("odu")
        if not odu or not odu.get("cycle"):
            return None

        cycle = dict(odu["cycle"])
        cycle["cap"] = cap
        cycle["days_left"] = None
        cycle["per_day"] = None
        cycle["today"] = None
        cycle["today_ts"] = None
        cycle["yesterday"] = None
        cycle["yesterday_ts"] = None
        cycle["avg_per_day"] = None
        cycle["projected_total"] = None

        # Airtel's own daily totals, not the ODU's own running counters -- the
        # SMS figures are what "this cycle"/"today" mean elsewhere in the app.
        cycle_days = db.carrier_days(cycle["start_ts"], int(time.time()))
        if cycle_days:
            cycle["today"] = cycle_days[-1]["bytes"]
            cycle["today_ts"] = cycle_days[-1]["ts"]
            # Only meaningful once yesterday is itself a full day inside this
            # cycle -- a cycle's first day has nothing to compare against.
            if len(cycle_days) >= 2:
                cycle["yesterday"] = cycle_days[-2]["bytes"]
                cycle["yesterday_ts"] = cycle_days[-2]["ts"]
            elapsed = len(cycle_days)
        else:
            elapsed = max(1, round((time.time() - cycle["start_ts"]) / 86400))
        if cycle["used"]:
            avg = cycle["used"] / elapsed
            cycle["avg_per_day"] = avg
            total_days = (cycle["end_ts"] - cycle["start_ts"]) / 86400
            cycle["projected_total"] = avg * total_days

        # The billing cycle immediately before this one, for a "vs last cycle"
        # comparison -- "prev_used" is trimmed to the same number of elapsed
        # days as the current cycle, so a partial cycle-to-date isn't measured
        # against a stranger's full month. "prev_total"/"prev_avg_per_day" use
        # the previous cycle's complete, final figures.
        cycle["prev_used"] = None
        cycle["prev_avg_per_day"] = None
        cycle["prev_total"] = None
        billing_day = self.server.config["alerts"].get("billing_day", 1)
        prev_anchor = datetime.date.fromtimestamp(cycle["start_ts"] - 86400)
        prev_start_ts, prev_end_ts = db.cycle_bounds(billing_day, prev_anchor)
        prev_days = db.carrier_days(prev_start_ts, prev_end_ts)
        if prev_days:
            cycle["prev_total"] = sum(d["bytes"] or 0 for d in prev_days)
            prev_total_days = (prev_end_ts - prev_start_ts) / 86400
            cycle["prev_avg_per_day"] = cycle["prev_total"] / prev_total_days
            cycle["prev_used"] = sum(d["bytes"] or 0 for d in prev_days[:elapsed])

        if not cap:
            return cycle

        # Airtel's own daily totals make the better burn-rate estimate where
        # there are enough of them: unlike the measured ledger they do not go
        # blank just because the PC was off.
        week_days = db.carrier_days(int(time.time()) - 7 * 86400)
        if len(week_days) >= 3:
            per_day = sum(d["bytes"] or 0 for d in week_days) / len(week_days)
        else:
            week = db.traffic_rows(int(time.time()) - 7 * 86400)
            if len(week) < 4:
                return cycle
            span = week[-1]["hour"] - week[0]["hour"] + 3600
            burned = sum(r["down"] + r["up"] for r in week)
            if span < 6 * 3600 or burned <= 0:
                return cycle
            per_day = burned / span * 86400

        cycle["per_day"] = per_day
        cycle["days_left"] = round(max(0, cap - cycle["used"]) / per_day, 1)
        return cycle

    def _diagnostics(self):
        """Latency to each hop, so a slowdown can be placed on the right link."""
        config = self.server.config
        snapshot = self.server.collector.snapshot()
        gateway = None
        if snapshot.get("odu"):
            gateway = snapshot["odu"]["wan"].get("mwan_wanlan1_wan_gateway")

        hops = [
            ("Indoor router", config["router"]["host"]),
            ("Outdoor unit", config["odu"]["host"]),
            ("Airtel gateway", gateway),
        ]
        results = [{"label": label, "host": host, "ms": _ping(host)}
                   for label, host in hops if host]

        # Some upstream hosts drop ICMP, so try a couple before calling it down.
        for host in ("8.8.8.8", "1.1.1.1"):
            latency = _ping(host)
            if latency is not None:
                results.append({"label": "Internet", "host": host, "ms": latency})
                break
        else:
            results.append({"label": "Internet", "host": "8.8.8.8", "ms": None})

        return results


ALLOWED_BUCKETS = {"5min", "hour", "day", "week", "month"}
RANGE_PRESETS = {"hour", "day", "week", "month", "cycle", "all"}


def _window(name, config, source=None):
    """(start timestamp, bucket size) for a named zoom level."""
    now = int(time.time())
    if name == "hour":
        return now - 3600, "5min"
    if name == "day":
        return now - 86400, "hour"
    if name == "week":
        return now - 7 * 86400, "day"
    if name == "month":
        return now - 30 * 86400, "day"
    if name == "cycle":
        start, _ = db.cycle_bounds(config["alerts"].get("billing_day", 1))
        return start, "day"
    if name == "all":
        # Each feed has its own idea of "the start" -- Airtel's texts and our
        # own measured ledger don't necessarily begin on the same day.
        since = db.carrier_tracked_since() if source == "carrier" else db.tracked_since()
        return since or now, "day"
    return now - 30 * 86400, "day"


def _resolve_range(query, config, source=None):
    """(since, until, default_bucket, label) for a preset or custom range."""
    now = int(time.time())
    range_name = (query.get("range") or [None])[0]

    if range_name == "custom":
        since_raw = (query.get("since") or [None])[0]
        until_raw = (query.get("until") or [None])[0]
        try:
            since = int(since_raw)
            until = int(until_raw) if until_raw else now
        except (TypeError, ValueError):
            raise ValueError("since/until must be unix timestamps")
        if since >= until:
            raise ValueError("since must be before until")
        span = until - since
        if span <= 3600:
            default_bucket = "5min"
        elif span <= 86400:
            default_bucket = "hour"
        elif span <= 30 * 86400:
            default_bucket = "day"
        elif span <= 366 * 86400:
            default_bucket = "week"
        else:
            default_bucket = "month"
        return since, until, default_bucket, "custom"

    name = range_name if range_name in RANGE_PRESETS else "month"
    since, default_bucket = _window(name, config, source)
    return since, now, default_bucket, name


def _bucket_start(ts, bucket):
    local = time.localtime(ts)
    if bucket == "week":
        local = time.localtime(ts - local.tm_wday * 86400)
        return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                0, 0, 0, 0, 0, -1)))
    if bucket == "month":
        return int(time.mktime((local.tm_year, local.tm_mon, 1,
                                0, 0, 0, 0, 0, -1)))
    if bucket == "day":
        return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                                0, 0, 0, 0, 0, -1)))
    if bucket == "5min":
        return ts - (ts % 300)
    return ts - (ts % 3600)


def _sample_deltas(samples):
    """Turn cumulative counter samples into per-sample traffic."""
    rows = []
    for previous, current in zip(samples, samples[1:]):
        down = (current["total_down"] or 0) - (previous["total_down"] or 0)
        up = (current["total_up"] or 0) - (previous["total_up"] or 0)
        if down < 0 or up < 0:
            continue  # the unit restarted between samples
        rows.append({"hour": current["ts"], "down": down, "up": up})
    return rows


def _speedtest(megabytes):
    """Pull a fixed-size file and time it.

    This spends real data from the plan, so the size is explicit and the caller
    is told how much was used.
    """
    total = megabytes * 1_000_000
    # Cloudflare answers 403 to the default urllib agent string.
    request = urllib.request.Request(
        "https://speed.cloudflare.com/__down?bytes=%d" % total,
        headers={"User-Agent": "Mozilla/5.0 (compatible; wifiapp)"},
    )
    started = time.time()
    read = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            read += len(chunk)
    elapsed = max(time.time() - started, 1e-6)
    return {"bytes": read, "seconds": round(elapsed, 2),
            "mbps": round(read * 8 / elapsed / 1_000_000, 1)}


def _ping(host, count=3):
    """Average round-trip in ms, or None when the host does not answer."""
    flag = "-n" if sys.platform == "win32" else "-c"
    try:
        completed = subprocess.run(
            ["ping", flag, str(count), host],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    times = [float(m) for m in re.findall(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms",
                                          completed.stdout)]
    if not times:
        return None
    # Windows prints a min/max/avg summary line too; averaging the per-reply
    # figures only would double-count it, so keep the first `count` samples.
    samples = times[:count]
    return round(sum(samples) / len(samples), 1)


def _lan_ip():
    """This PC's LAN-facing address.

    Opens a UDP "connection" (no packet actually sent) to pick whichever
    local interface the OS would route through -- the standard trick for
    finding your own LAN IP without depending on hostname resolution.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _lan_url(port):
    """This PC's LAN-facing address, for phones on the same WiFi to reach."""
    ip = _lan_ip()
    return "http://%s:%d" % (ip, port) if ip else None


def main():
    config = load_config()
    collector = Collector(config)
    collector.start()

    bind = config["server"]["bind"]
    port = config["server"]["port"]

    # Windows lets a second process bind an already-listening port when
    # SO_REUSEADDR is set, which leaves a stale server quietly answering. Turn
    # it off so starting twice fails loudly instead.
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer((bind, port), Handler)
    except OSError:
        print("Port %d is already in use -- is the dashboard already running?" % port)
        collector.stop()
        return
    server.collector = collector
    server.config = config

    print("Dashboard on http://localhost:%d" % port)
    print("From your phone on the same WiFi, use your PC's LAN IP and port %d." % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        collector.stop()
        server.server_close()


if __name__ == "__main__":
    main()
