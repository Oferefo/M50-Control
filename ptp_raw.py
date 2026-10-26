"""Minimal raw PTP-over-USB client, used only for what libgphoto2 cannot send:
writing Canon EOS properties that have no gphoto2 config widget (movie
quality, 0xD1CD). gphoto2 must have released the camera before this runs.
"""

import struct

import usb.backend.libusb1
import usb.core
import usb.util

CANON_VID = 0x04A9
LIBUSB = "/opt/homebrew/lib/libusb-1.0.dylib"

CMD, DATA, RESP = 1, 2, 3
RC_OK = 0x2001

OC_OPEN_SESSION = 0x1002
OC_CLOSE_SESSION = 0x1003
OC_EOS_SET_REMOTE_MODE = 0x9114
OC_EOS_SET_EVENT_MODE = 0x9115
OC_EOS_SET_PROP_EX = 0x9110


class PTPError(Exception):
    pass


class PTPSession:
    def __init__(self, timeout_ms=5000):
        backend = usb.backend.libusb1.get_backend(find_library=lambda _: LIBUSB)
        self.dev = usb.core.find(idVendor=CANON_VID, backend=backend)
        if self.dev is None:
            raise PTPError("Canon camera not found on USB")
        self.timeout = timeout_ms
        intf = self.dev.get_active_configuration()[(0, 0)]
        self.intf = intf.bInterfaceNumber
        ep = lambda d, t: usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == d
            and usb.util.endpoint_type(e.bmAttributes) == t)
        self.ep_out = ep(usb.util.ENDPOINT_OUT, usb.util.ENDPOINT_TYPE_BULK)
        self.ep_in = ep(usb.util.ENDPOINT_IN, usb.util.ENDPOINT_TYPE_BULK)
        usb.util.claim_interface(self.dev, self.intf)
        self.tid = 0

    def close(self):
        try:
            usb.util.release_interface(self.dev, self.intf)
        finally:
            usb.util.dispose_resources(self.dev)

    def _send(self, kind, code, payload):
        self.ep_out.write(struct.pack("<IHHI", 12 + len(payload), kind, code, self.tid) + payload,
                          self.timeout)

    def _read_container(self):
        buf = bytes(self.ep_in.read(512, self.timeout))
        length = struct.unpack_from("<I", buf)[0]
        while len(buf) < length:
            buf += bytes(self.ep_in.read(max(512, length - len(buf)), self.timeout))
        kind, code, _tid = struct.unpack_from("<HHI", buf, 4)
        return kind, code, buf[12:length]

    def transaction(self, code, params=(), data=None):
        """Run one PTP transaction; returns (response code, data received)."""
        self.tid += 1
        self._send(CMD, code, struct.pack("<%dI" % len(params), *params))
        if data is not None:
            self._send(DATA, code, data)
        received = b""
        while True:
            kind, rcode, payload = self._read_container()
            if kind == DATA:
                received += payload
            elif kind == RESP:
                return rcode, received

    def check(self, code, params=(), data=None):
        rc, received = self.transaction(code, params, data)
        if rc != RC_OK:
            raise PTPError(f"PTP op 0x{code:04x} failed: 0x{rc:04x}")
        return received


def set_eos_prop(propcode, value):
    """Open a session, write one EOS property (raw value bytes), close."""
    s = PTPSession()
    try:
        s.check(OC_OPEN_SESSION, (1,))
        try:
            # same handshake libgphoto2 uses before talking to an EOS body
            s.check(OC_EOS_SET_REMOTE_MODE, (1,))
            s.check(OC_EOS_SET_EVENT_MODE, (1,))
            payload = struct.pack("<II", 8 + len(value), propcode) + value
            s.check(OC_EOS_SET_PROP_EX, data=payload)
        finally:
            s.transaction(OC_CLOSE_SESSION)
    finally:
        s.close()
