"""Background poller.

Runs on its own thread, samples both devices on a fixed interval, writes history
to SQLite and keeps the most recent reading in memory so the dashboard can render
without waiting on the hardware. Also raises the events the alerting panel shows:
link drops and recoveries, data-cap thresholds and signal degradation.
"""

import threading
import time
import traceback
from collections import deque

from . import db, sms
from .odu import Odu, OduError, parse_carriers, signal_grade
from .router import Router, RouterError


class Collector:
    def __init__(self, config):
        db.init()  # before anything reads a remembered setting below
        self.config = config
        self.interval = config["collector"]["poll_seconds"]
        self.retention_days = config["collector"]["retention_days"]
        self.alerts = config["alerts"]

        odu_cfg = config["odu"]
        self.odu = Odu(
            odu_cfg["host"], odu_cfg["username"], odu_cfg["password"],
            scheme=db.get_setting("odu_login_scheme"),
            on_scheme=lambda name: db.set_setting("odu_login_scheme", name))
        router_cfg = config["router"]
        self.router = Router(router_cfg["host"], router_cfg["password"])

        # Both devices answer in tens of milliseconds, so the speed readouts get
        # their own once-a-second loop while the heavier radio/SMS/history work
        # stays on the slower one.
        self.live_interval = config["collector"].get("live_seconds", 1)
        self._history_every = max(1, round(self.interval / max(self.live_interval, 0.1)))
        self._fast_ticks = 0
        self._dev_history = {}   # mac -> [(ts, down_bytes, up_bytes)], for real rates

        self.latest = {"odu": None, "devices": None, "live": None, "errors": {}}
        # Last few minutes of speed readings, so a browser opening the
        # dashboard for the first time still sees a populated chart rather
        # than one that fills in over the next three minutes.
        self._live_history = deque(maxlen=200)
        self.lock = threading.Lock()
        self._stop = threading.Event()

        # Alert state, so each threshold fires once rather than every poll.
        self._was_connected = None
        self._fired_ratios = set()
        self._low_signal_since = None
        self._last_prune = 0.0

        # Settings that change only when someone changes them; no point asking
        # the ODU for these on every poll.
        self._settings = {}
        self._settings_at = 0.0

        # SMS, likewise: a text every few hours does not need a five-second poll.
        self._sms = []
        self._sms_at = 0.0
        self._sms_top_id = None

    def start(self):
        threads = [
            threading.Thread(target=self._loop, daemon=True, name="collector"),
            threading.Thread(target=self._live_loop, daemon=True, name="collector-live"),
        ]
        for thread in threads:
            thread.start()
        return threads

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll_once()
            except Exception:
                # A single bad poll must never kill the thread.
                traceback.print_exc()
            elapsed = time.time() - started
            self._stop.wait(max(1.0, self.interval - elapsed))

    def _live_loop(self):
        while not self._stop.is_set():
            started = time.time()
            try:
                self._poll_live()
            except Exception:
                traceback.print_exc()
            elapsed = time.time() - started
            self._stop.wait(max(0.2, self.live_interval - elapsed))

    def _poll_live(self):
        """The fast loop: the outdoor unit's speeds, once a second.

        The ODU really does recompute its speed every second, so this is worth
        doing. The indoor router does not -- its per-client table only moves
        every twenty or thirty seconds -- so the client list is fetched on the
        slower beat instead, and its speeds are worked out here from the byte
        counters rather than trusted as reported.
        """
        now = time.time()
        live = {"ts": int(now)}

        try:
            session = self.odu.usage("session")
            live.update({
                "down_speed": _int(session.get("real_rx_speed")),
                "up_speed": _int(session.get("real_tx_speed")),
                "session_down": _int(session.get("real_rx_bytes")),
                "session_up": _int(session.get("real_tx_bytes")),
            })
            self._set_error("odu", None)
        except (OduError, OSError) as exc:
            self._set_error("odu", str(exc))

        self._fast_ticks += 1
        # First tick as well, so the dashboard has a client list straight away.
        due = self._fast_ticks == 1 or self._fast_ticks % self._history_every == 0

        with self.lock:
            self.latest["live"] = live
            if "down_speed" in live:
                self._live_history.append(
                    {"ts": live["ts"], "down": live["down_speed"], "up": live["up_speed"]})

        if due:
            self._poll_devices(now)

    def _poll_devices(self, now):
        """Client list, byte counters and history, on the slow beat."""
        try:
            devices = self.router.devices()
            self._set_error("router", None)
        except (RouterError, OSError, ValueError) as exc:
            self._set_error("router", str(exc))
            return

        self._measure_device_speeds(now, devices)

        stamp = int(now)
        db.record_devices(stamp, devices)
        db.accumulate({"dev:" + d["mac"]: (d["down_bytes"], d["up_bytes"])
                       for d in devices if d.get("mac") and d["counters_ok"]},
                      stamp)

        with self.lock:
            self.latest["devices"] = {"ts": stamp, "items": devices}

    def _measure_device_speeds(self, now, devices):
        """Replace the firmware's stale speed field with a measured rate.

        The router publishes a per-client speed, but it recomputes the whole
        station table on its own slow timer, so that figure sits frozen for
        half a minute at a time and then jumps. Deltas of the byte counters
        over a rolling window give the same answer without the staircase --
        and where the counters have saturated there is nothing to measure, so
        the firmware's number is left alone and flagged.
        """
        window = max(30.0, self.interval * 3)
        for device in devices:
            mac = device.get("mac")
            device["speed_measured"] = False
            if not mac or not device["counters_ok"]:
                continue

            history = self._dev_history.setdefault(mac, [])
            history.append((now, device["down_bytes"], device["up_bytes"]))
            while len(history) > 2 and now - history[0][0] > window:
                history.pop(0)

            oldest = history[0]
            span = now - oldest[0]
            if span < self.interval:
                continue
            down = device["down_bytes"] - oldest[1]
            up = device["up_bytes"] - oldest[2]
            if down < 0 or up < 0:          # the router restarted its counters
                del history[:-1]
                continue
            device["down_speed"] = int(down / span)
            device["up_speed"] = int(up / span)
            device["speed_measured"] = True

    def _set_error(self, source, message):
        with self.lock:
            if message:
                self.latest["errors"][source] = message
            else:
                self.latest["errors"].pop(source, None)

    def _poll_once(self):
        now = int(time.time())

        odu_snapshot = None
        try:
            odu_snapshot = self._poll_odu(now)
            self._set_error("odu", None)
        except (OduError, OSError) as exc:
            self._set_error("odu", str(exc))
            self._handle_connectivity(now, connected=False)

        with self.lock:
            if odu_snapshot is not None:
                self.latest["odu"] = odu_snapshot

        if time.time() - self._last_prune > 3600:
            db.prune(self.retention_days)
            self._last_prune = time.time()

    def _poll_odu(self, now):
        netinfo = self.odu.netinfo()
        carriers = parse_carriers(netinfo)
        primary = carriers[0] if carriers else {}

        session = self.odu.usage("session")
        month = self.odu.usage("month")
        total = self.odu.usage("total")

        wan = {}
        try:
            wan = self.odu.wan_status()
        except OduError:
            pass

        connected = str(wan.get("current_wan_status", "")).startswith("ipv4_connected")
        rsrp = primary.get("rsrp")

        db.record_signal({
            "ts": now,
            "rsrp": rsrp,
            "rsrq": primary.get("rsrq"),
            "sinr": primary.get("sinr"),
            "band": netinfo.get("wan_active_band"),
            "cell_id": _int(netinfo.get("cell_id")),
            "pci": _int(netinfo.get("lte_pci")),
            "earfcn": _int(netinfo.get("wan_active_channel")),
            "network_type": netinfo.get("network_type"),
            "carriers": len(carriers),
            "connected": 1 if connected else 0,
        })

        usage_row = {
            "ts": now,
            "session_down": _int(session.get("real_rx_bytes")),
            "session_up": _int(session.get("real_tx_bytes")),
            "month_down": _int(month.get("month_rx_bytes")),
            "month_up": _int(month.get("month_tx_bytes")),
            "total_down": _int(total.get("total_rx_bytes")),
            "total_up": _int(total.get("total_tx_bytes")),
            "down_speed": _int(session.get("real_rx_speed")),
            "up_speed": _int(session.get("real_tx_speed")),
        }
        db.record_usage(usage_row)

        # The lifetime counter is the one the ODU never resets, so it is the
        # most reliable thing to take deltas from.
        db.accumulate({"wan": (usage_row["total_down"], usage_row["total_up"])}, now)
        cycle = db.cycle_usage(self.config["alerts"].get("billing_day", 1))

        self._handle_connectivity(now, connected)
        self._handle_data_cap(cycle["used"])
        self._handle_signal(now, rsrp)

        if now - self._settings_at > 300:
            try:
                self._settings = {"auto_reset": self.odu.auto_reset(),
                                  "counters_cleared": self.odu.counters_cleared_on(),
                                  "qos": self.odu.qos_settings()}
                self._settings_at = now
            except OduError:
                pass

        if now - self._sms_at > 120:
            self._poll_sms(now)

        return {
            "ts": now,
            "netinfo": netinfo,
            "carriers": carriers,
            "grade": signal_grade(rsrp),
            "usage": usage_row,
            "cycle": cycle,
            "settings": self._settings,
            "msisdn": db.carrier_msisdn(),
            "session_time": _int(session.get("real_time")),
            "wan": wan,
            "connected": connected,
        }

    def _poll_sms(self, now):
        """Read the inbox, and mine it for Airtel's daily usage figures."""
        try:
            raw = self.odu.sms_list()
        except (OduError, OSError):
            return
        self._sms_at = now

        messages = [sms.normalise(m) for m in raw]
        db.record_carrier_usage([m["usage"] for m in messages if m["usage"]])
        self._prune_usage_sms(messages, now)

        top = max((m["id"] for m in messages if m["id"] is not None), default=None)
        if self._sms_top_id is not None and top is not None and top > self._sms_top_id:
            for message in messages:
                if message["id"] > self._sms_top_id and not message["usage"]:
                    # Usage texts arrive daily and are not news; anything else is.
                    db.log_event("sms", "%s: %s" % (message["from"],
                                                    message["body"][:120]))
        self._sms_top_id = top if top is not None else self._sms_top_id
        self._sms = messages

    def _prune_usage_sms(self, messages, now):
        """Free the modem's inbox: a usage text is safe in the DB the moment
        it's polled, so it does not need to keep taking up a slot on-device.

        The modem holds ~100 messages total and Airtel sends one a day, so
        without this it would eventually fill up -- and a full inbox means
        the network holds or drops new texts rather than the modem making
        room, which is exactly what the Data tab's carrier feed depends on
        never happening. A 14-day buffer is kept so this stays well clear
        of the 120s poll interval.
        """
        cutoff = now - 14 * 86400
        stale = [m for m in messages if m["usage"] and m["ts"] and m["ts"] < cutoff]
        if not stale:
            return
        for message in stale:
            try:
                self.odu.sms_delete(message["id"])
            except (OduError, OSError):
                pass
        db.log_event("sms_prune", "Freed %d usage text%s already saved to history"
                     % (len(stale), "" if len(stale) == 1 else "s"))

    # -- alerting ----------------------------------------------------------

    def _handle_connectivity(self, now, connected):
        if self._was_connected is None:
            self._was_connected = connected
            return
        if connected != self._was_connected:
            db.log_event("link_up" if connected else "link_down",
                         "WAN reported %s" % ("connected" if connected else "down"))
            self._was_connected = connected

    def _handle_data_cap(self, used):
        cap = self.alerts.get("data_cap_bytes")
        if not cap:
            return
        for ratio in self.alerts.get("warn_ratios", []):
            if used >= cap * ratio and ratio not in self._fired_ratios:
                self._fired_ratios.add(ratio)
                db.log_event(
                    "data_cap",
                    "Month-to-date usage passed %d%% of the %s cap"
                    % (ratio * 100, human_bytes(cap)),
                )

    def _handle_signal(self, now, rsrp):
        threshold = self.alerts.get("rsrp_warn_dbm")
        if rsrp is None or threshold is None:
            return
        if rsrp < threshold:
            if self._low_signal_since is None:
                self._low_signal_since = now
            elif now - self._low_signal_since > 300:
                db.log_event("weak_signal",
                             "RSRP held below %d dBm for 5 minutes (now %.0f)"
                             % (threshold, rsrp))
                self._low_signal_since = now + 1800  # rate-limit repeats
        else:
            self._low_signal_since = None

    # -- accessors ---------------------------------------------------------

    def snapshot(self):
        with self.lock:
            return {
                "odu": self.latest["odu"],
                "devices": self.latest["devices"],
                "live": self.latest["live"],
                "errors": dict(self.latest["errors"]),
            }

    def live(self):
        """The once-a-second view: speeds only, small enough to poll hard."""
        with self.lock:
            live = dict(self.latest["live"] or {})
            devices = (self.latest["devices"] or {}).get("items") or []
            live["errors"] = dict(self.latest["errors"])
        live["devices"] = [
            {"mac": d["mac"], "down_speed": d["down_speed"], "up_speed": d["up_speed"],
             "down_bytes": d["down_bytes"], "up_bytes": d["up_bytes"],
             "counters_ok": d["counters_ok"],
             "speed_measured": d.get("speed_measured", False)}
            for d in devices
        ]
        return live

    def live_history(self):
        """Recent speed readings, oldest first, for pre-populating the chart."""
        with self.lock:
            return list(self._live_history)

    def note_qos_change(self, enable, priority):
        """Update the cached QoS reading right after a successful write, so
        the dashboard reflects the new Optimise-for mode without waiting on
        the next 300s settings refresh."""
        self._settings = dict(self._settings, qos={
            "qos_smart_switch": 1 if enable else 0,
            "qos_smart_pri_type": int(priority),
        })

    def messages(self):
        return list(self._sms)

    def refresh_sms(self):
        self._poll_sms(int(time.time()))
        return self.messages()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def human_bytes(count):
    if count is None:
        return "-"
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "%.2f %s" % (size, unit)
        size /= 1024
