"""One exception type for "the hardware said no", whichever hardware it is.

The dashboard talks to two different families of device (see ``hardware.py``)
and the callers up in ``collector`` and ``app`` do not want to know which one
they got. They catch ``DeviceError``; each client raises its own subclass.
"""


class DeviceError(Exception):
    pass
