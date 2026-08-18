"""One-off: switch the radio mode, and put it back if the link does not return.

Works against either supported family -- the mode names differ, so run it with
no argument to see the ones this unit accepts.
"""

import json
import sys
import time

from airtel_odu_app.core import db, hardware

GRACE = 90

config = json.load(open("config.json", encoding="utf-8"))
db.init()
odu, _, kind = hardware.build(config)
odu.login()

modes = odu.net_modes
before = odu.netinfo()
previous = before.get("net_select")

if len(sys.argv) < 2:
    print("usage: python switch_mode.py <mode>\n\nmodes this %s unit accepts:" % kind)
    for mode, label in modes.items():
        print("  %-14s %s%s" % (mode, label, "   <- in use" if mode == previous else ""))
    raise SystemExit(1)

TARGET = sys.argv[1]
if TARGET not in modes:
    raise SystemExit("unknown mode %r -- run with no argument to list them" % TARGET)

print("current mode : %s (%s)" % (previous, modes.get(previous, "?")))
print("target mode  : %s (%s)" % (TARGET, modes.get(TARGET, "?")))
print("network type : %s" % before.get("network_type"))

odu.set_network_mode(TARGET)
print("\nsent. waiting for the link to come back...")

deadline = time.time() + GRACE
while time.time() < deadline:
    time.sleep(5)
    try:
        odu.login()
        info = odu.netinfo()
    except Exception as exc:
        print("  %2ds  %s" % (int(time.time() - deadline + GRACE), exc))
        continue
    print("  %2ds  mode=%-14s type=%-10s rsrp=%s nr5g_rsrp=%s"
          % (int(time.time() - deadline + GRACE), info.get("net_select"),
             info.get("network_type"), info.get("lte_rsrp"), info.get("nr5g_rsrp")))
    if info.get("network_type") not in (None, "", "NO_SERVICE", "Limited_Service"):
        print("\nlink is back on %s." % info.get("network_type"))
        break
else:
    print("\nno link after %ds -- reverting to %s" % (GRACE, previous))
    odu.set_network_mode(previous)
