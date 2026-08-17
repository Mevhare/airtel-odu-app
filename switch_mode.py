"""One-off: switch the ODU radio mode, and put it back if the link does not return."""

import json
import sys
import time

from airtel_odu_app.core import db
from airtel_odu_app.core.odu import NET_MODES, Odu

TARGET = sys.argv[1] if len(sys.argv) > 1 else "LTE_AND_5G"
GRACE = 90

config = json.load(open("config.json", encoding="utf-8"))
db.init()
odu = Odu(**config["odu"],
          scheme=db.get_setting("odu_login_scheme"),
          on_scheme=lambda name: db.set_setting("odu_login_scheme", name))
odu.login()

before = odu.netinfo()
previous = before.get("net_select")
print("current mode : %s (%s)" % (previous, NET_MODES.get(previous, "?")))
print("target mode  : %s (%s)" % (TARGET, NET_MODES.get(TARGET, "?")))
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
