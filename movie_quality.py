"""Read the M50 movie recording quality (PTP prop 0xD1CD, "MovieParam4").

libgphoto2 has no config widget for it, but the camera reports its value and
its allowed options in EOS events, and libgphoto2 hex-dumps those into its
debug log. We listen to that log; nothing extra is sent to the camera.

Layout (little-endian u32s), worked out from this camera's own reports and
checked against its menu:
  option entry, 36 bytes: [0x24, 0, size, fps*100, code, 0, 3, 1, 0]
  current value, 44 bytes: [0x2c, 0, size, fps*100, code, 0, 3, 1, 0, 0, ?]
size: 5 = 4K, 0 = FHD, 1 = HD.
"""

import re
import struct
import threading

import gphoto2 as gp

SIZES = {5: ("4K", "3840x2160"), 0: ("FHD", "1920x1080"), 1: ("HD", "1280x720")}

_HEAD = re.compile(r"prop d1cd (value|options) changed")
_HEX = re.compile(r"^\s*0x[0-9a-f]+:\s+(.*)$")


def _hex_bytes(line):
    m = _HEX.match(line)
    if not m:
        return None
    return bytes(0 if t == "-" else int(t, 16) for t in m.group(1).split())


def _u32s(data):
    n = len(data) // 4
    return list(struct.unpack("<%dI" % n, data[: n * 4]))


def describe(size, fps100):
    short, res = SIZES.get(size, (f"?{size}", "?"))
    return {"size": size, "fps100": fps100, "label": f"{short} {fps100 / 100:.2f}P", "resolution": res}


class MovieQualityReader:
    def __init__(self):
        self.current = None  # describe() dict + "raw" u32 list
        self.options = []  # list of describe() dicts + "raw"
        self._lock = threading.Lock()
        self._collecting = None  # "value" | "options"
        self._buf = b""
        self._hook = None

    def install(self):
        # python-gphoto2 removes the hook when this object is dropped
        self._hook = gp.gp_log_add_func(gp.GP_LOG_DEBUG, self._on_log)

    def _on_log(self, level, domain, text, data=None):
        for line in str(text).splitlines():
            head = _HEAD.search(line)
            if head:
                self._finish()
                self._collecting = head.group(1)
                self._buf = b""
                continue
            if self._collecting:
                chunk = _hex_bytes(line)
                if chunk is None:
                    self._finish()
                else:
                    self._buf += chunk

    def _finish(self):
        kind, buf = self._collecting, self._buf
        self._collecting, self._buf = None, b""
        if not kind or not buf:
            return
        words = _u32s(buf)
        with self._lock:
            if kind == "value" and len(words) >= 4:
                self.current = dict(describe(words[2], words[3]), raw=words)
            elif kind == "options":
                opts = []
                for i in range(0, len(words) - 8, 9):
                    e = words[i : i + 9]
                    if e[0] == 0x24:
                        opts.append(dict(describe(e[2], e[3]), raw=e))
                if opts:
                    self.options = opts

    def snapshot(self):
        with self._lock:
            strip = lambda d: {k: v for k, v in d.items() if k != "raw"} if d else None
            return {"current": strip(self.current), "options": [strip(o) for o in self.options]}
