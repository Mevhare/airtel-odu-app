# WiFi Console

A local dashboard for the Airtel outdoor unit (ODU) and indoor CPE. Runs on your
PC, reads both devices directly, stores history in SQLite. No cloud, no account,
no dependencies beyond Python 3.

```bash
python app.py
```

Then open <http://localhost:8080>. To reach it from a phone on the same WiFi, use
your PC's LAN address instead of `localhost` — run `ipconfig` to find it.

## What it shows

Five tabs. Everything a person actually asks about is on the first four; the
radio internals live behind an **Advanced** disclosure in Settings, closed by
default.

| Tab | Contents | Source |
| --- | --- | --- |
| Home | Online/offline, technology and band, live throughput chart, device count, uptime, cycle usage, recent activity | ODU `nwinfo_get_netinfo` + `zwrt_data.get_wwandst` |
| Data | Cycle bar and projection, session/rolled/lifetime counters, per-hour usage chart, ranked per-device usage | the local ledger (below) + CPE `station_list` |
| Devices | Every connected device, ranked by usage, with measured speed and signal | CPE `station_list` |
| Texts | The modem's inbox, with select / mark read / delete | ODU `zwrt_wms` |
| Settings | Network mode picker, APN (editable), connection test, speed test, the usage-mismatch explanation, and Advanced | `zwrt_apn_object` |
| Settings → Advanced | Signal bars and plain-English quality, carrier aggregation, radio detail, modem console, restart buttons | `lteca`/`ltecasig`, `run_at_process`, `system.reboot` |

The charts have axes and hover readouts: the live chart is one point per second
over the last five minutes, newest at the right edge; the usage chart is one bar
per hour. Each entry in Recent activity opens to a plain sentence explaining what
the event meant.

Two tests are built in. **Connection test** pings each hop (router → ODU →
Airtel gateway → internet) and resolves a hostname, so a slowdown can be placed
on the right link. **Speed test** downloads 10 MB through the connection — it
spends 10 MB of the allowance each run, which the UI says plainly.

## Why device usage and data usage do not match

They are two different measurements and are not meant to agree.

The per-device figures come from the **indoor router**, which counts only WiFi
frames it has forwarded, and its counters reset when it restarts (and when a
client leaves and rejoins). The Data tab's figures come from the **outdoor
unit**, which counts every byte over the mobile link since the unit was
installed — wired traffic, background traffic, protocol overhead, and every
device that has ever connected and since gone.

So the device list is for comparing devices against each other. The outdoor
unit's counters are the ones that relate to the plan's allowance.

## Recent activity

The activity list on Home is the log of things that happened to the connection
while the dashboard was running: the link dropping and returning, the signal
falling below `alerts.rsrp_warn_dbm`, allowance thresholds being crossed, and any
change made from Settings. It is deliberately empty most of the time — it exists
so that "the WiFi was bad last night" can be answered with a time and a duration
instead of a guess. It is stored in `wifi.db` and survives restarts.

## Configuration

Copy `config.example.json` to `config.json` before first run — it's gitignored,
so your credentials never leave your machine.

```bash
cp config.example.json config.json
```

Everything lives in `config.json`.

- `odu` / `router` — host and password for each device. These stay on your machine.
- `alerts.data_cap_bytes` — your plan's cap. Default is 50 GB.
- `alerts.billing_day` — day of the month the cycle resets (1–28).
- `safety.allow_writes` — **false by default.** While false, the app is strictly
  read-only and the network-mode, APN and restart controls are disabled.

## Why usage is computed locally

The ODU exposes three counters: session, "month" and lifetime. The "month" counter
is never actually reset — on this unit it currently holds over 250 days and 9 TB of
traffic — so a cap checked against it is meaningless.

Neither device keeps a history. Both offer only counters that run forever, so
"how much did I use on Tuesday between 3 and 4" is not a question either box can
answer — it can only be built by watching. That is exactly what the collector
does: it samples the counters, turns each pair of samples into a delta, and files
the delta in an hourly ledger (`traffic`), keyed by hour and source. Totals,
projections and the per-hour chart all read from the ledger, and the raw samples
stay in `usage_samples` so the ledger can be re-derived and checked against them.

A delta that spans a sampling gap — the PC asleep, the app stopped — is spread
across the hours it actually covers rather than dumped into the hour the app woke
up in (`_spread` in `core/db.py`). Without that, an overnight gap put two hours of
traffic into a single bucket and made the chart lie about *when*, though never
about *how much*.

The cap bar, projection and default Data-tab view now report Airtel's own daily
SMS figures (`carrier_usage`) instead: unlike the local ledger, they don't go
blank when the PC was off. The measured ledger is kept as a fallback for days
before the first SMS arrives, and stays selectable in the Data tab as
"Measured" for cross-checking.

## Firmware quirks worth knowing

These cost real time to find, so they are worth recording:

- **`multi_data=1` breaks list commands.** `station_list` returns the full array
  for `cmd=station_list`, but an empty *string* for `cmd=station_list&multi_data=1`.
  This is why the connected-device list looks unsupported until you drop the
  parameter. Scalar commands are the opposite and want `multi_data=1`.
- **Most CPE scalars answer without a session.** `cr_version` and
  `psw_fail_num_str` respond to anonymous callers, so they cannot be used to test
  whether you are logged in. `station_list` can — it returns a list once
  authenticated and `""` before.
- **CPE login** is `SHA256(SHA256(password).upper() + LD.upper()).upper()`, where
  `LD` comes from `cmd=LD`. The POST result code is not a reliable success signal,
  so the client verifies by reading a session-gated value.
- **ODU calls must be a JSON array**, even for a single call, and want
  `{"source_module": "web", "cid": 1}` style arguments. Passing `{}` returns
  result code `2` (invalid argument) rather than an error message.
- **ODU login** normally wraps the password in an RSA envelope, but only when the
  device serves a certificate. This unit returns an empty `crt`, so the salt from
  `zwrt_web.web_login_info` is enough. The client tries the known hash schemes and
  remembers whichever the firmware accepts — by *name*, in the `settings` table,
  because the token itself is salt-bound and worthless after a reboot. On this
  unit the winner is `sha-upper-salt-upper`, the first candidate, so nothing is
  wasted; the memory matters on a build where it is not.
- **The ODU locks out after repeated logins**, and restarting the app twice
  inside a minute was enough to trigger it once — five minutes with the unit
  refusing every login. `login_fail_lock_lefttime` reports the seconds left, so
  the client now records the deadline and holds off locally rather than knocking
  once a second until it expires.
- **Device byte counters are 32-bit** and wrap negative past ~2 GB; they are
  folded back into unsigned range. Past saturation they stop moving altogether —
  such a device is flagged `counters_ok: false` and left out of the ledger rather
  than reported wrongly.
- **The router's station table is not live.** Both the speeds and the byte totals
  in `station_list` are recomputed by the firmware only every 20–30 seconds:
  measured, per-device speeds sat identical across 18 seconds while the WAN
  swung from 0.3 to 5.5 MB/s. Polling it faster buys nothing, so the router stays
  on the slow beat and each device's rate is computed here from byte deltas over
  a rolling window (`speed_measured` marks the ones derived that way).
- **Only one router session exists at a time.** A second login anywhere kicks the
  first out, which shows up as fields going `None` mid-poll. Do not run a probe
  script against the CPE while the dashboard is up.
- **Counter direction is from the router's point of view** — `tx_*` is
  router-to-device, i.e. what the device *downloaded*.
- **Do not start the server twice on Windows.** `SO_REUSEADDR` lets a second
  process bind an already-listening port, leaving a stale server answering with
  old code. `allow_reuse_address` is disabled so this now fails loudly.

## How often things are actually read

Two loops. The fast one asks the ODU for its speeds and session counters once a
second — that is genuine, not decoration: the unit's own timestamp advances by
exactly one per second and it answers in about 10 ms. The slow one
(`collector.poll_seconds`) does the heavier work: radio detail, the device list,
SMS, history and the ledger.

The router is deliberately on the slow loop, because it cannot go faster (see the
station-table quirk above). So the Home tab's throughput is per-second truth from
the ODU, while per-device speeds are half-minute averages computed here — the
Devices tab says so rather than implying otherwise.

## When one box is up and the other is not

The dashboard talks to two devices and either can go away on its own, so nothing
depends on both being present. Errors are tracked per source, and the front end
has a state for each case:

- ODU unreachable — Home says so, the WiFi/device half keeps working.
- ODU stale — if the last reading is over 30 seconds old it is labelled with the
  time it was taken instead of being shown as current.
- Router unreachable — the device list stays visible, headed with the time it was
  last true.

## APN

Readable from the ODU without touching its web UI, and writable from
Settings → Connection settings. Lives in the `zwrt_apn_object` namespace
(`modify_manu_apn` / `set_apn_mode` / `enable_manu_apn_id`).

Client-wide DNS was investigated and is **not achievable** on this hardware —
tried, not just assumed. The ODU is in IP passthrough: its real `lan` DHCP
section is disabled and hands out nothing, and the one live section
(`lan_ippt`, feeding the CPE) hands clients Airtel's resolvers regardless.
`zwrt_router.api.router_set_lan_dns` looks like the fix — right name, right
argument shape — but is a stub: proven by writing real values against the
disabled `lan` section (a target with no live effect) and reading `uci get
dhcp` back afterward; the stored value never moved. Direct `uci.set` on the
`dhcp` config is separately blocked by the ACL (`-32002 Access denied`). The
CPE (indoor router, `192.168.18.1`) can't cover for it either: it has no SIM
of its own (`modem_main_state: modem_undetected`) and its only DNS-shaped
fields (`prefer_dns_manual` etc.) belong to an inactive cellular APN profile,
never wired to any setter that reaches its DHCP server. Per-device static DNS
is the only lever that actually works here.

APN writes carry real risk — the modem drops connectivity while it re-dials on
the new profile — so the form warns about that and reuses the same revert
safety net as network-mode switching: `_arm_apn_revert` in `app.py` waits
`safety.band_switch_revert_seconds`, and if the link has not come back it
restores the previous profile automatically. `set_apn` only overwrites the
fields a person would actually mean to change (APN, username, password, PDP
type, auth mode) and carries the rest of the existing profile over untouched.

## The modem console

`run_at_process` takes `{"cmd": "..."}` — not `at_cmd`, which is why it returns
code 2 if you guess. It reaches the modem directly, so it is not limited to what
the stock web UI exposes.

The server only accepts **query forms** (no `=` assignments), because AT commands
can reconfigure or disable the radio. Useful reads:

```
AT+CSQ          signal quality
ATI             modem model and firmware
AT+COPS?        registered operator and access technology
AT+CGCONTRDP    bearer / APN details
```

The modem is MediaTek (`MOLY.NR16...`), so MediaTek AT syntax applies —
Quectel-style commands such as `AT+QENG` return `+CME ERROR: 4`.

## Writes

`safety.allow_writes` gates the network-mode switch, QoS priority, APN, and
the restart buttons. It's `false` by default, so a fresh install is strictly
read-only until you turn it on.

### Network modes

The ODU takes exactly these `net_select` values. They are not guesses — they are
read from the unit's own `js/config/cpe/MC8830/config.js` (`AUTO_MODES`), and the
stock UI sends the same strings to `zte_nwinfo_api.nwinfo_set_netselect`:

| Value | Means |
| --- | --- |
| `WL_AND_5G` | 5G + 4G + 3G, automatic |
| `LTE_AND_5G` | 5G NSA — 5G carrier anchored on 4G **(in use)** |
| `Only_5G` | 5G standalone only |
| `WCDMA_AND_LTE` | 4G + 3G |
| `Only_LTE` | 4G only |
| `Only_WCDMA` | 3G only |

Switching drops the link for 10–30 seconds. The app arms an automatic revert: if
the connection has not returned within `safety.band_switch_revert_seconds`, the
previous mode is restored and the attempt is logged.

`switch_mode.py` does the same thing from the command line with its own revert
guard, for when the dashboard is not running:

```bash
python switch_mode.py LTE_AND_5G
```

Those UI files are only readable **after logging in** — the ODU answers
`405 URL Not Allowed` for anything under `js/auth/` without the `webtoken`
cookie that `zwrt_web.web_login` sets. Log in with a cookie jar installed and the
whole front end becomes readable, which is how the mode list was recovered.

## Layout

```
app.py              HTTP server, JSON API, static hosting
switch_mode.py      command-line radio mode switch with revert guard
core/odu.py         ubus client for the outdoor unit
core/router.py      goform client for the indoor CPE
core/collector.py   background poller, alerting
core/db.py          SQLite schema, the hourly ledger, queries
web/                dashboard (no build step, no libraries)
wifi.db             history; delete it to start over
```
