# Airtel ODU App

A local dashboard for Airtel outdoor unit (ODU) + indoor router setups. See
your signal, usage, and connected devices at a glance, and fix problems
without digging through the modem's clunky admin page.

Runs entirely on your own PC. No cloud, no account, no sign-up — it just
talks to your two devices over your own WiFi and shows you what's going on.

## Features

**Home** — Is the internet up right now? What technology (4G/5G) and band is
it on? A live throughput chart, how many devices are connected, and a log of
recent events (drops, weak signal, data-cap warnings) so "the WiFi was bad
last night" has an actual answer.

**Data usage** — How much data you've used this billing cycle, projected
against your plan, with a daily/weekly/monthly breakdown and per-device
ranking. Export to CSV any time.

**Devices** — Every device on your WiFi, ranked by usage, with live speed and
signal strength. Your own device gets tagged automatically.

**Texts** — Your modem's SMS inbox (data-usage alerts, carrier notices) as a
normal-looking conversation thread instead of a raw list.

**Optimise for** — One tap to retune the connection for gaming (steadier,
lower-latency, deprioritises everything else) or performance (best available
speed). Switches network mode and QoS together, with an automatic safety net
that reverts the change if the connection doesn't come back.

**Settings** — Network mode picker, editable APN, a connection test and a
speed test, and an Advanced section for people who want the radio internals
(carrier aggregation, signal detail, a modem AT-command console, restart
buttons).

**Add to phone** — A QR code that opens the dashboard straight from your
phone's camera, for whoever's not at the PC.

## Getting started

```bash
pip install airtel-odu-app
airtel-odu-app
```

Then open <http://localhost:8080> and log in with the same admin password you'd
use on the ODU/router's own web page — that's it, no files to edit.

To reach the dashboard from your phone, use your PC's LAN address instead of
`localhost` (shown in the terminal when it starts), or scan the QR code in
Settings.

### Running from source

```bash
git clone https://github.com/Mevhare/airtel-odu-app.git
cd airtel-odu-app
pip install -e .
airtel-odu-app
```

## Built for

An Airtel fixed-wireless setup with a ZTE outdoor unit (ODU) and a ZTE MF296A
indoor router, talking to each other at their fixed LAN addresses —
`192.168.254.1` for the ODU, `192.168.18.1` for the router. That pairing is
what this app was built and tested against; other ZTE-based Airtel ODU/router
combos will likely work too since they share the same underlying firmware.

## Requirements

- Python 3.9+, nothing else — no external packages.
- An Airtel ODU + indoor CPE router setup, both reachable on your LAN.

## Safety

The app is **read-only by default**. Turning on `safety.allow_writes` in
`config.json` is what enables the network-mode switch, QoS, APN edits, and
restart buttons — leave it off if you just want to watch, not touch anything.

Every write that can drop your connection (mode switch, APN change) carries
an automatic revert: if the link doesn't come back within a short window,
the previous setting is restored on its own.

## License

MIT — see [LICENSE](LICENSE).
