# Airtel ODU App

A local dashboard for your Airtel outdoor unit (ODU) and indoor router. See
your signal, data usage, and connected devices at a glance, and fix problems
without digging through the modem's clunky admin page.

Runs entirely on your own PC. No cloud, no account, no sign-up — it just
talks to your two devices over your own WiFi and shows you what's going on.

## Features

**Home** — Is the internet up right now? What network type (4G/5G) is it
using? A live speed chart, how many devices are connected, and a log of
recent events (drops, weak signal, data-cap warnings) so "the WiFi was bad
last night" has an actual answer.

**Data usage** — How much data you've used this billing cycle, projected
against your plan, broken down by day/week/month and by device. Export to
CSV any time.

**Devices** — Every device on your WiFi, ranked by usage, with live speed and
signal strength. Your own device gets tagged automatically.

**Texts** — Your modem's SMS inbox (data-usage alerts, carrier notices) shown
as a normal-looking conversation instead of a raw list.

**Optimise for** — One tap to retune the connection for gaming (steadier,
lower-latency) or performance (best available speed). If the connection
doesn't come back afterwards, it automatically switches back on its own.

**Settings** — Network mode picker, editable APN (the setting that connects
you to Airtel's network), a connection test and a speed test, and an Advanced
section with extra detail for anyone curious (signal strength breakdown, a
low-level modem console, restart buttons).

**Add to phone** — A QR code that opens the dashboard straight from your
phone's camera, for whoever's not at the PC.

## Getting started

```bash
pip install airtel-odu-app
airtel-odu-app
```

Then open <http://localhost:8080> and log in with the same admin password you'd
use on the device's own web page — that's it, no files to edit. (If you have
the two-box ZTE setup you're asked for both passwords; if you have the
single-box ZLT unit, just the one.)

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

Two kinds of Airtel hardware, both tested against the real thing. The app
figures out which one you have automatically — you don't have to tell it.

**ZTE** — an outdoor unit at `192.168.254.1` plus an indoor router (ZTE
MF296A) at `192.168.18.1`. Two boxes, two admin passwords. Other ZTE-based
Airtel setups will likely work too.

**ZLT** — a ZLT/Tozed unit such as the **X17U** (with its W154R Plus indoor
router), where one box does everything at `192.168.1.1`. One address, one
password.

ZLT units can do a bit less than the ZTE setup — no traffic-priority
controls, and their monthly usage counter always resets on the same day each
month (that's fixed by the device, not adjustable). Per-device usage still
works the same either way, since the app tracks that itself rather than
relying on the router. The dashboard only shows the controls your specific
device can actually do — nothing that would fail if you tapped it.

If it ever guesses your hardware wrong, tell it directly in `config.json`:

```json
"device": "zlt",
"zlt": { "host": "192.168.1.1" }
```

`"device"` can be `auto` (the default), `zte`, or `zlt`.

## Requirements

- Python 3.9 or newer — nothing else to install.
- One of the two hardware setups above, on your home network.

## Safety

Changing network mode, traffic priority, APN, or restarting the device — all
of that is switched on by default. If you'd rather the app just watch and
never change anything, set `safety.allow_writes` to `false` in `config.json`.

Any change that could drop your connection (mode switch, APN change) reverts
itself automatically if the connection doesn't come back within a few
seconds.

## Releasing (maintainers)

Publishing to PyPI happens automatically: bump `version` in `pyproject.toml`,
then create a [GitHub Release](https://github.com/Mevhare/airtel-odu-app/releases/new)
for that version (tag it `vX.Y.Z`) and the
[publish workflow](.github/workflows/publish.yml) builds and uploads it.

That workflow uses PyPI's [Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
so no API token lives in this repo. One-time setup on PyPI (needs a PyPI
account, done once by whoever owns the `airtel-odu-app` name there): add a
pending publisher pointing at this repo, workflow file `publish.yml`, and
environment `pypi`.

## License

MIT — see [LICENSE](LICENSE).
