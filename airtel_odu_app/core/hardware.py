"""Which hardware is on the other end, and the pair of clients that talks to it.

Two families are supported and they share nothing but the interface:

  ``zte``  the original Airtel pair -- a ZTE MF296A-family outdoor unit
           speaking ubus at one address, and an indoor CPE speaking goform at
           another. Two devices, two logins.
  ``zlt``  a ZLT/Tozed unit such as the X17U, where the outdoor and indoor
           halves share one web interface at one address. One device, one login.

``device: "auto"`` in config.json probes for the ZLT interface first (it answers
in milliseconds and cannot be mistaken for anything else) and falls back to the
ZTE pair, which is what the addresses in a config file written before this
existed will describe.
"""

from . import db
from .odu import Odu
from .router import Router
from .zlt import ZltOdu, ZltRouter, ZltSession, probe

ZTE = "zte"
ZLT = "zlt"


def build(config):
    """(odu, router, kind) for whatever this config points at."""
    kind = (config.get("device") or "auto").strip().lower()

    if kind == ZTE:
        return _zte(config) + (ZTE,)
    if kind == ZLT:
        return _zlt(config, _zlt_host(config)) + (ZLT,)
    if kind != "auto":
        raise ValueError("device must be 'auto', 'zte' or 'zlt', not %r" % (kind,))

    for host in _candidates(config):
        if probe(host):
            print("Found a ZLT-style device at %s." % host)
            return _zlt(config, host) + (ZLT,)
    return _zte(config) + (ZTE,)


def _zte(config):
    odu_config = config["odu"]
    odu = Odu(
        odu_config["host"], odu_config.get("username", "admin"),
        odu_config["password"],
        scheme=db.get_setting("odu_login_scheme"),
        on_scheme=lambda name: db.set_setting("odu_login_scheme", name))
    router_config = config["router"]
    return odu, Router(router_config["host"], router_config["password"])


def _zlt(config, host):
    """Both faces of a single unit, over one shared session.

    Its password rides in the ``odu`` block, which is the one the dashboard's
    own login screen writes first -- so a single-device unit needs no separate
    router entry, and a config file already carrying one is left alone.
    """
    odu_config = config["odu"]
    link = ZltSession(host, odu_config.get("username", "admin"),
                      odu_config["password"])
    return ZltOdu(link), ZltRouter(link)


def _zlt_host(config):
    return ((config.get("zlt") or {}).get("host")
            or config["router"]["host"] or config["odu"]["host"])


def _candidates(config):
    """Addresses to try, nearest guess first, without asking any of them twice."""
    seen = []
    for host in ((config.get("zlt") or {}).get("host"),
                 config["router"]["host"], config["odu"]["host"]):
        if host and host not in seen:
            seen.append(host)
    return seen
