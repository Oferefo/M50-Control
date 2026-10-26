"""Canon EOS M50 controller over USB (libgphoto2).

One worker thread owns the camera. Everything else talks to it through
CameraController.call(), so USB access is never concurrent.
"""

import json
import logging
import os
import queue
import re
import struct
import subprocess
import threading
import time
from concurrent.futures import Future

import gphoto2 as gp

import ptp_raw
from movie_quality import MovieQualityReader

log = logging.getLogger("m50")

CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
# survives a crash/restart of the app (libgphoto2 can segfault on odd events)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
PERSISTED = ("want_recording", "auto_restart", "split_minutes", "download_photos",
             "schedule_start", "schedule_stop", "restart_count")

# Settings shown in the UI, in display order.
SETTINGS = [
    ("iso", "ISO"),
    ("shutterspeed", "מהירות תריס"),
    ("aperture", "צמצם"),
    ("exposurecompensation", "פיצוי חשיפה"),
    ("whitebalance", "איזון לבן"),
    ("picturestyle", "Picture Style"),
    ("focusmode", "מצב פוקוס"),
    ("afmethod", "שיטת AF"),
    ("continuousaf", "AF רציף"),
    ("movieservoaf", "Movie Servo AF"),
    ("imageformat", "פורמט תמונה"),
    ("drivemode", "מצב צילום"),
    ("liveviewsize", "גודל תצוגה חיה"),
]


class CameraError(Exception):
    pass


def usb_holders():
    """Names of other processes that have the camera's USB device open.
    Google Chrome doing this while we started a recording hung the M50."""
    try:
        out = subprocess.run(["ioreg", "-l", "-w0", "-r", "-n", "Canon Digital Camera"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    me = os.getpid()
    names = set()
    for pid, name in re.findall(r'"IOUserClientCreator" = "pid (\d+), ([^"]+)"', out):
        if int(pid) != me:
            names.add(name)
    return sorted(names)


class CameraController:
    def __init__(self):
        self.cam = None
        self.connected = False
        self.info = {}
        self.error = None

        # recording state
        self.want_recording = False  # user asked to record
        self.recording = False  # camera reports it is recording
        self.rec_started_at = None
        self.auto_restart = True
        self.split_minutes = 0  # 0 = never split
        self.restart_count = 0
        self._not_rec_count = 0
        self._restart_failures = 0

        # schedule (epoch seconds or None). The scheduled stop is final:
        # it ends recording even when auto-restart is on.
        self.schedule_start = None
        self.schedule_stop = None
        self._next_sched_try = 0.0
        self._connect_timeouts = 0

        # live view
        self.preview_on = False
        self.preview_frame = None
        self.preview_seq = 0
        self.frame_ready = threading.Condition()
        # live view pauses around record start/stop: a frame grab right before
        # the record command was in play when the camera hung at 59.94P
        self._preview_hold_until = 0.0
        self._preview_failures = 0

        self.download_photos = True
        self.files = []  # newest first: {folder, name, time, local}
        self.events = []  # newest first: {time, text}
        self.raw_events = []  # newest first, camera events gphoto2 doesn't decode
        self.movie_quality = MovieQualityReader()

        self._q = queue.Queue()
        self._stopped = False
        self.usb_conflicts = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        self._saved_state = None
        self._load_state()

    def start(self):
        self.movie_quality.install()
        self._thread.start()

    # ---------- public API (any thread) ----------

    def call(self, fn, *args, timeout=30):
        """Run fn(*args) on the camera thread and return its result."""
        fut = Future()
        self._q.put((fn, args, fut))
        return fut.result(timeout=timeout)

    def status(self):
        with self._lock:
            return {
                "connected": self.connected,
                "error": self.error,
                "info": dict(self.info),
                "want_recording": self.want_recording,
                "recording": self.recording,
                "rec_seconds": int(time.time() - self.rec_started_at) if self.rec_started_at else 0,
                "auto_restart": self.auto_restart,
                "split_minutes": self.split_minutes,
                "restart_count": self.restart_count,
                "schedule_start": self.schedule_start,
                "schedule_stop": self.schedule_stop,
                "now": time.time(),
                "usb_conflicts": self.usb_conflicts,
                "movie_quality": self.movie_quality.snapshot(),
                "preview_on": self.preview_on,
                "download_photos": self.download_photos,
                "files": self.files[:20],
                "events": self.events[:30],
            }

    def note(self, text):
        log.info(text)
        with self._lock:
            self.events.insert(0, {"time": time.strftime("%H:%M:%S"), "text": text})
            del self.events[100:]

    # ---------- camera-thread commands ----------

    def cmd_start_recording(self):
        self._require()
        if not self.info.get("movie_mode"):
            raise CameraError("המצלמה לא במצב וידאו — סובב את החוגה למצב וידאו")
        self.want_recording = True
        self.restart_count = 0
        try:
            self._start_recording()
        except Exception:
            self.want_recording = False  # a failed manual start is not a restart case
            raise

    def cmd_stop_recording(self):
        self._require()
        self.want_recording = False
        self._stop_recording()

    def set_schedule(self, start, stop):
        """start/stop are epoch seconds or None. Safe from any thread."""
        now = time.time()
        if start is not None and start <= now:
            raise CameraError("שעת ההתחלה כבר עברה")
        if stop is not None and stop <= max(now, start or 0):
            raise CameraError("שעת הסיום חייבת להיות אחרי שעת ההתחלה")
        self.schedule_start, self.schedule_stop = start, stop
        self._next_sched_try = 0.0
        parts = []
        if start:
            parts.append("התחלה " + time.strftime("%H:%M", time.localtime(start)))
        if stop:
            parts.append("סיום " + time.strftime("%H:%M", time.localtime(stop)))
        self.note("תזמון נקבע: " + ", ".join(parts) if parts else "התזמון בוטל")

    def _load_state(self):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return
        for key in PERSISTED:
            if key in state:
                setattr(self, key, state[key])
        self._saved_state = state
        if self.want_recording or self.schedule_start or self.schedule_stop:
            self.note("שוחזר מצב קודם (צילום/תזמון) אחרי הפעלה מחדש")

    def _save_state(self):
        state = {key: getattr(self, key) for key in PERSISTED}
        if state == self._saved_state:
            return
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
        self._saved_state = state

    def _check_schedule(self):
        now = time.time()
        if self.schedule_stop and now >= self.schedule_stop:
            self.schedule_stop = None
            self.schedule_start = None
            self.want_recording = False  # final stop, overrides auto-restart
            self.note("⏹ סיום מתוזמן — עוצר סופית")
            if self.recording:
                self._stop_recording()
            return
        if self.schedule_start and now >= self.schedule_start and now >= self._next_sched_try:
            if self.recording:
                self.schedule_start = None
                self.want_recording = True
                return
            self.note("▶️ התחלה מתוזמנת")
            try:
                self.cmd_start_recording()
                self.schedule_start = None
            except CameraError as e:
                # keep trying until it works or the stop time arrives
                self._next_sched_try = now + 5
                self.note(f"התחלה מתוזמנת נכשלה, מנסה שוב: {e}")

    def cmd_take_photo(self):
        self._require()
        if self.info.get("movie_mode"):
            raise CameraError("המצלמה במצב וידאו — סובב את החוגה למצב צילום סטילס (P/Av/Tv/M/Auto)")
        self._set("capturetarget", "Memory card", quiet=True)
        path = self.cam.capture(gp.GP_CAPTURE_IMAGE)
        self.note(f"צולמה תמונה: {path.name}")
        self._file_added(path.folder, path.name, photo=True)
        return path.name

    def cmd_set_setting(self, name, value):
        self._require()
        self._set(name, value)
        self._refresh_info()

    def cmd_get_settings(self):
        self._require()
        cfg = self.cam.get_config()
        out = []
        for key, label in SETTINGS:
            try:
                w = cfg.get_child_by_name(key)
            except gp.GPhoto2Error:
                continue
            choices = []
            if w.get_type() in (gp.GP_WIDGET_RADIO, gp.GP_WIDGET_MENU):
                choices = [w.get_choice(i) for i in range(w.count_choices())]
            out.append({
                "key": key,
                "label": label,
                "value": str(w.get_value()),
                "choices": choices,
                "readonly": bool(w.get_readonly()) or len(choices) < 2,
            })
        return out

    def cmd_set_preview(self, on):
        self.preview_on = bool(on)
        if not on:
            self.preview_frame = None
            if self.connected and not self.recording:
                self._end_pc_live_view()

    def cmd_sync_clock(self):
        self._require()
        self._set("syncdatetime", 1)
        self.note("שעון המצלמה סונכרן למחשב")

    def cmd_autofocus(self):
        self._require()
        self._set("autofocusdrive", 1)
        self._pump_events(1500)
        # an AF drive left open keeps the camera "busy" for record/capture
        self._release_af()

    def cmd_download(self, folder, name):
        self._require()
        return self._download(folder, name)

    def cmd_set_movie_quality(self, size, fps100):
        """gphoto2 can't write movie quality (0xD1CD), so release the camera,
        write it over raw PTP, and reconnect. Only one of the options the
        camera itself offers is ever sent, based on its own current value."""
        self._require()
        if self.recording or self.want_recording:
            raise CameraError("אי אפשר לשנות איכות וידאו בזמן צילום")
        # the hangs first blamed on this write all happened while Chrome also
        # held the camera's USB device — never write while anything else does
        self.usb_conflicts = usb_holders()
        if self.usb_conflicts:
            raise CameraError(f"{', '.join(self.usb_conflicts)} מחזיק את המצלמה — סגור אותו לפני שינוי איכות")
        mq = self.movie_quality
        cur = mq.current
        opt = next((o for o in mq.options if o["size"] == size and o["fps100"] == fps100), None)
        if not cur or not opt:
            raise CameraError("לא ידועה איכות הווידאו הנוכחית / האפשרות לא קיימת במצלמה")
        if (cur["size"], cur["fps100"]) == (size, fps100):
            return cur["label"]
        raw = list(cur["raw"])
        raw[2:5] = opt["raw"][2:5]  # size, fps, code — the rest stays as the camera sent it
        value = struct.pack("<%dI" % len(raw), *raw)

        preview = self.preview_on
        self.preview_on = False
        self._disconnect()
        time.sleep(0.5)
        try:
            ptp_raw.set_eos_prop(0xD1CD, value)
        except (ptp_raw.PTPError, OSError) as e:
            raise CameraError(f"שינוי איכות וידאו נכשל: {e}")
        finally:
            time.sleep(1)
            self._connect()
            self.preview_on = preview
        self.note(f"איכות וידאו: {cur['label']} ← {opt['label']}")
        return opt["label"]

    def cmd_reconnect(self):
        self._disconnect()

    def cmd_shutdown(self):
        """Close the PTP session cleanly before the app exits. Killing the app
        mid live-view frame left the M50 hung until a power pull."""
        self._stopped = True
        self.preview_on = False
        if self.connected:
            if not self.recording:
                self._end_pc_live_view()
            self._pump_events(200)
        self._disconnect()

    # ---------- internals ----------

    def _require(self):
        if not self.connected:
            raise CameraError("המצלמה לא מחוברת")

    def _set(self, name, value, quiet=False, tries=3):
        last = None
        for _ in range(tries):
            try:
                cfg = self.cam.get_config()
                w = cfg.get_child_by_name(name)
                w.set_value(value)
                self.cam.set_single_config(name, w)
                return
            except gp.GPhoto2Error as e:
                last = e
                if e.code != gp.GP_ERROR_CAMERA_BUSY:
                    break
                self._pump_events(500)
        if quiet:
            log.warning("set %s=%s failed: %s", name, value, last)
            return
        raise CameraError(f"לא הצלחתי לשנות {name}: {last}")

    def _get(self, name):
        return str(self.cam.get_config().get_child_by_name(name).get_value())

    def _release_af(self):
        self._set("autofocusdrive", 0, quiet=True, tries=1)
        self._set("cancelautofocus", 1, quiet=True, tries=1)
        self._set("eosremoterelease", "Release Full", quiet=True, tries=1)
        self._pump_events(300)

    def _end_pc_live_view(self):
        """Hand live view back to the camera so its own menu stays usable.
        Never touch "output" (EVF output device): on the M50 any change to it
        makes the next record command hang the camera until a battery pull."""
        self._set("viewfinder", 0, quiet=True, tries=1)

    def _start_recording(self):
        self._preview_hold_until = time.time() + 10
        try:
            self._start_recording_inner()
        finally:
            self._preview_hold_until = time.time() + 2

    def _start_recording_inner(self):
        self.usb_conflicts = usb_holders()
        if self.usb_conflicts:
            raise CameraError(f"{', '.join(self.usb_conflicts)} מחזיק את המצלמה — סגור אותו לפני צילום "
                              "(אחרת המצלמה נתקעת)")
        self._release_af()
        # The M50 answers "Device Busy" unless live view is running first.
        self._set("viewfinder", 1, quiet=True)
        self._pump_events(1500)
        self._set("capturetarget", "Memory card", quiet=True)
        for attempt in range(4):
            try:
                self._set("movierecordtarget", "Card", tries=2)
            except CameraError:
                self._pump_events(1000)
                continue
            if self._wait_value("movierecordtarget", "Card", 3):
                self.recording = True
                self.rec_started_at = time.time()
                self._not_rec_count = 0
                self.note("הצילום התחיל")
                return
        self.recording = False
        raise CameraError("המצלמה לא התחילה לצלם (בדוק כרטיס זיכרון / מצב וידאו)")

    def _stop_recording(self):
        self._preview_hold_until = time.time() + 2
        try:
            self._set("movierecordtarget", "None")
        finally:
            self.recording = False
            self.rec_started_at = None
        self.note("הצילום נעצר")
        if not self.preview_on and not self.want_recording:
            self._pump_events(500)
            self._end_pc_live_view()

    def _wait_value(self, name, value, seconds):
        end = time.time() + seconds
        while time.time() < end:
            if self._get(name) == value:
                return True
            self._pump_events(200)
        return False

    def _connect(self):
        # macOS' ptpcamerad grabs every PTP camera; it must let go first.
        subprocess.run(["pkill", "-9", "ptpcamerad"], capture_output=True)
        time.sleep(0.2)
        cam = gp.Camera()
        cam.init()
        self.cam = cam
        self.connected = True
        self.error = None
        self._pump_events(300)
        self._refresh_info()
        self.recording = self._get("movierecordtarget") == "Card"
        if self.recording and not self.rec_started_at:
            self.rec_started_at = time.time()
        if not self.recording:
            # On connect the M50 routes its display to "MOBILE2" and blanks its
            # screen. Starting the viewfinder (the camera's own Initiate/
            # TerminateViewfinder ops, not a write to "output") brings it back —
            # the same thing record-start does, after which the screen works.
            self._set("viewfinder", 1, quiet=True)
            self._pump_events(1000)
            if not self.preview_on:
                self._end_pc_live_view()
        self.note(f"מחובר: {self.info.get('model', 'Canon')}")

    def _disconnect(self):
        if self.cam:
            try:
                self.cam.exit()
            except gp.GPhoto2Error:
                pass
        self.cam = None
        if self.connected:
            self.note("המצלמה התנתקה")
        self.connected = False
        self.recording = False

    def _refresh_info(self):
        cfg = self.cam.get_config()

        def val(n):
            try:
                return str(cfg.get_child_by_name(n).get_value())
            except gp.GPhoto2Error:
                return ""

        dial = val("autoexposuremodedial")
        info = {
            "model": val("cameramodel"),
            "battery": val("batterylevel"),
            "lens": val("lensname"),
            "dial": dial,
            "shots": val("availableshots"),
            "movie_mode": val("eosmovieswitch") == "1" or dial == "Movie",
        }
        try:
            # standard PTP GetStorageInfo; the M50 has one SD slot
            sd = self.cam.get_storageinfo()[0]
            info["card_free_mb"] = sd.freekbytes // 1024
            info["card_total_mb"] = sd.capacitykbytes // 1024
        except (gp.GPhoto2Error, IndexError):
            info["card_free_mb"] = self.info.get("card_free_mb")
            info["card_total_mb"] = self.info.get("card_total_mb")
        with self._lock:
            self.info = info

    def _pump_events(self, ms):
        """Read camera events for up to ms milliseconds (always at least one
        poll: with tiny ms the first wait used to round down to 0 and skip
        reading, so live view mode never saw recording/file events)."""
        end = time.time() + ms / 1000
        first = True
        while True:
            left = int((end - time.time()) * 1000)
            if left <= 0 and not first:
                return
            first = False
            typ, data = self.cam.wait_for_event(max(1, min(left, 100)))
            if typ == gp.GP_EVENT_FILE_ADDED:
                self._file_added(data.folder, data.name)
            elif typ == gp.GP_EVENT_TIMEOUT:
                return
            elif typ == gp.GP_EVENT_UNKNOWN and data:
                self.raw_events.insert(0, f"{time.strftime('%H:%M:%S')} {data}")
                del self.raw_events[300:]

    def _file_added(self, folder, name, photo=False):
        with self._lock:
            if any(f["name"] == name and f["folder"] == folder for f in self.files):
                return
            entry = {"folder": folder, "name": name, "time": time.strftime("%H:%M:%S"), "local": None}
            self.files.insert(0, entry)
            del self.files[200:]
        if name.upper().endswith(".MP4"):
            self.note(f"קובץ וידאו נשמר בכרטיס: {name}")
        if photo and self.download_photos:
            try:
                entry["local"] = self._download(folder, name)
            except gp.GPhoto2Error as e:
                self.note(f"הורדה נכשלה: {e}")

    def _download(self, folder, name):
        target = os.path.join(CAPTURE_DIR, name)
        f = self.cam.file_get(folder, name, gp.GP_FILE_TYPE_NORMAL)
        f.save(target)
        for e in self.files:
            if e["folder"] == folder and e["name"] == name:
                e["local"] = target
        return target

    def _watch_recording(self):
        """Detect the camera stopping on its own and restart if wanted."""
        cam_rec = self._get("movierecordtarget") == "Card"
        if cam_rec:
            self._not_rec_count = 0
            if not self.recording:
                self.recording = True
                self.rec_started_at = self.rec_started_at or time.time()
            if (self.want_recording and self.split_minutes
                    and time.time() - self.rec_started_at >= self.split_minutes * 60):
                self.note(f"פיצול קובץ אחרי {self.split_minutes} דק'")
                self._stop_recording()
                self._pump_events(1500)
                self._start_recording()
            return

        self._not_rec_count += 1
        if self._not_rec_count < 2:  # ignore a single odd reading
            return
        if self.recording:
            self.recording = False
            self.rec_started_at = None
            if self.want_recording:
                self.note("⚠️ המצלמה עצרה את הצילום בעצמה")
        if self.want_recording and self.auto_restart:
            self.restart_count += 1
            self.note(f"מתחיל צילום מחדש (#{self.restart_count})")
            self._pump_events(1000)
            try:
                self._start_recording()
                self._restart_failures = 0
            except CameraError as e:
                self._restart_failures += 1
                self.note(f"התחלה מחדש נכשלה: {e}")
                # with a scheduled stop pending, keep trying until that time
                if self._restart_failures >= 5 and not self.schedule_stop:
                    self.want_recording = False
                    self._restart_failures = 0
                    self.note("מוותר על התחלה מחדש אחרי 5 ניסיונות")
        elif not self.auto_restart:
            self.want_recording = False

    def _run(self):
        last_watch = 0.0
        last_info = 0.0
        while True:
            self._save_state()
            if self._stopped:
                try:
                    fn, args, fut = self._q.get(timeout=0.5)
                    fut.set_exception(CameraError("האפליקציה נסגרת"))
                except queue.Empty:
                    pass
                continue
            if not self.connected:
                try:
                    self._connect()
                except gp.GPhoto2Error as e:
                    self.error = f"אין חיבור למצלמה ({e})"
                    self.cam = None
                    self._drain_queue_with_error()
                    if e.code == gp.GP_ERROR_TIMEOUT:
                        # a session left open (e.g. app killed mid-transfer)
                        # keeps the camera mute until the USB port is reset
                        self._connect_timeouts += 1
                        if self._connect_timeouts % 2 == 0:
                            self.note("המצלמה לא עונה — מאפס את חיבור ה-USB")
                            subprocess.run(["pkill", "-9", "ptpcamerad"], capture_output=True)
                            subprocess.run(["gphoto2", "--reset"], capture_output=True, timeout=30)
                    time.sleep(2)
                    continue
                self._connect_timeouts = 0

            try:
                try:
                    fn, args, fut = self._q.get_nowait()
                except queue.Empty:
                    fn = None
                if fn:
                    try:
                        fut.set_result(fn(*args))
                    except gp.GPhoto2Error as e:
                        fut.set_exception(CameraError(str(e)))
                        if e.code in (gp.GP_ERROR_IO, gp.GP_ERROR_TIMEOUT, gp.GP_ERROR_IO_USB_CLAIM):
                            raise
                    except Exception as e:
                        fut.set_exception(e)
                    continue

                now = time.time()
                if self.preview_on and now >= self._preview_hold_until:
                    self._grab_preview_frame()
                if now - last_watch > 1:
                    last_watch = now
                    self._check_schedule()
                    self._watch_recording()
                if now - last_info > 10:
                    last_info = now
                    self._refresh_info()
                    conflicts = usb_holders()
                    if conflicts and conflicts != self.usb_conflicts:
                        self.note(f"⚠️ {', '.join(conflicts)} מחזיק את חיבור ה-USB של המצלמה")
                    self.usb_conflicts = conflicts
                self._pump_events(1 if self.preview_on else 100)
            except gp.GPhoto2Error as e:
                self.note(f"שגיאת תקשורת: {e} — מתחבר מחדש")
                self._disconnect()
                time.sleep(1)

    def _grab_preview_frame(self):
        """Grab one live view frame. The camera refuses frames while its menu
        is open or it is powering down; that is not a lost connection, so
        back off instead of reconnecting (reconnect storms can hang the M50)."""
        try:
            pf = self.cam.capture_preview()  # keep alive while copying
            frame = bytes(memoryview(pf.get_data_and_size()))
        except gp.GPhoto2Error as e:
            if e.code in (gp.GP_ERROR_IO, gp.GP_ERROR_TIMEOUT, gp.GP_ERROR_IO_USB_CLAIM):
                raise
            self._preview_failures += 1
            if self._preview_failures >= 10:
                self.preview_on = False
                self.preview_frame = None
                self._preview_failures = 0
                self.note("התצוגה החיה הושהתה — המצלמה לא שולחת תמונה (תפריט פתוח?)")
            else:
                self._pump_events(500)
            return
        self._preview_failures = 0
        with self.frame_ready:
            self.preview_frame = frame
            self.preview_seq += 1
            self.frame_ready.notify_all()

    def _drain_queue_with_error(self):
        while True:
            try:
                _, _, fut = self._q.get_nowait()
            except queue.Empty:
                return
            fut.set_exception(CameraError(self.error or "המצלמה לא מחוברת"))
