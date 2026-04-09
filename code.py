import json
import time

import adafruit_httpserver
import adafruit_ntp
import board
import busio
import mdns
import microcontroller
import neopixel
import rtc
import socketpool
import sys
import wifi
from adafruit_httpserver import POST, PUT, FileResponse

from animation import ClockAnimationCore, ClockPlatform, load_core_class_from_source, wheel

IS_AI_THINKER_CAM = "Ai Thinker ESP32-CAM with ESP32" in sys.implementation._machine
if IS_AI_THINKER_CAM:
    PIXEL_PIN = board.IO13
elif "FooToy" in sys.implementation._machine:
    PIXEL_PIN = board.IO16
else:
    PIXEL_PIN = board.IO0
PIXEL_COUNT = 60
BRIGHTNESS = 0.2
SERVER_PORT = 81
MDNS_HOSTNAME = "ledclock"
MAX_CONSECUTIVE_NTP_FAILURES = 5
UI_ROOT = "/ui"
AMBIENT_SAMPLE_INTERVAL_S = 5
MIN_PIXEL_BRIGHTNESS = 0.03
MAX_PIXEL_BRIGHTNESS = 0.8
BRIGHTNESS_SMOOTHING = 0.25
PIXEL_INDEX_OFFSET = 29
AMBIENT_SETTINGS_FILE = "ambient_settings.json"
DEFAULT_AMBIENT_SETTINGS = {
    "dark_luma": 0.0,
    "bright_luma": 1.0,
    "min_pixel_brightness": MIN_PIXEL_BRIGHTNESS,
    "max_pixel_brightness": MAX_PIXEL_BRIGHTNESS,
    "sample_interval_s": float(AMBIENT_SAMPLE_INTERVAL_S),
    "brightness_smoothing": BRIGHTNESS_SMOOTHING,
    "manual_agc_gain": None,
    "manual_aec_value": None,
}

# WiFi credentials
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise


def day_of_week(year, month, day):
    """Calculate day of week using Zeller's congruence.
    Returns: 0=Sunday, 1=Monday, ..., 6=Saturday
    """
    if month < 3:
        month += 12
        year -= 1

    q = day
    m = month
    k = year % 100
    j = year // 100
    h = (q + ((13 * (m + 1)) // 5) + k + (k // 4) + (j // 4) - (2 * j)) % 7
    return (h + 6) % 7


def find_nth_weekday(year, month, weekday, n):
    first_day_of_month = day_of_week(year, month, 1)
    first_occurrence = 1 + (weekday - first_day_of_month) % 7
    return first_occurrence + (n - 1) * 7


def is_dst(year, month, day, hour):
    if month < 3 or month > 11:
        return False

    if month > 3 and month < 11:
        return True

    if month == 3:
        dst_start_day = find_nth_weekday(year, 3, 0, 2)
        if day < dst_start_day:
            return False
        if day > dst_start_day:
            return True
        return hour >= 2

    if month == 11:
        dst_end_day = find_nth_weekday(year, 11, 0, 1)
        if day < dst_end_day:
            return True
        if day > dst_end_day:
            return False
        return hour < 2

    return False


def get_pacific_tz_offset():
    current = time.localtime()
    if is_dst(current.tm_year, current.tm_mon, current.tm_mday, current.tm_hour):
        return -7
    return -8


def sync_time():
    print("Syncing time via NTP...")
    pool = socketpool.SocketPool(wifi.radio)

    ntp = adafruit_ntp.NTP(pool, tz_offset=0)
    rtc.RTC().datetime = ntp.datetime

    tz_offset = get_pacific_tz_offset()
    ntp = adafruit_ntp.NTP(pool, tz_offset=tz_offset)
    rtc.RTC().datetime = ntp.datetime

    tz_name = "PDT" if tz_offset == -7 else "PST"
    print(f"Time synced ({tz_name}): {time.localtime()}")


def connect_wifi():
    print("Connecting to WiFi...")
    wifi.radio.connect(secrets["ssid"], secrets["password"])
    print(f"Connected to WiFi! IP: {wifi.radio.ipv4_address}")


def start_mdns():
    print(f"Starting mDNS as {MDNS_HOSTNAME}.local")
    mdns_server = mdns.Server(wifi.radio)
    mdns_server.hostname = MDNS_HOSTNAME
    mdns_server.advertise_service(
        service_type="_http", protocol="_tcp", port=SERVER_PORT
    )
    print(f"mDNS HTTP service available at http://{MDNS_HOSTNAME}.local:{SERVER_PORT}")
    return mdns_server


class PixelOffsetWrapper:
    def __init__(self, pixels, offset):
        self._pixels = pixels
        self._offset = offset

    def __len__(self):
        return len(self._pixels)

    def _map_index(self, index):
        return (index + self._offset) % len(self._pixels)

    def __getitem__(self, index):
        return self._pixels[self._map_index(index)]

    def __setitem__(self, index, value):
        self._pixels[self._map_index(index)] = value

    def show(self):
        self._pixels.show()

    @property
    def brightness(self):
        return self._pixels.brightness

    @brightness.setter
    def brightness(self, value):
        self._pixels.brightness = value


class AmbientLightController:
    def __init__(self, pixels):
        self.pixels = pixels
        self.camera = None
        self.settings_path = AMBIENT_SETTINGS_FILE
        self.settings = self._load_settings()
        self.persisted_settings = dict(self.settings)
        self.last_sample_monotonic = -self.settings["sample_interval_s"]
        self.last_luma = None
        self.last_target_brightness = pixels.brightness
        self.last_applied_brightness = pixels.brightness

        if not IS_AI_THINKER_CAM:
            print("Ambient light sensing disabled: board has no built-in camera")
            return

        try:
            import espcamera
        except ImportError as exc:
            print(f"Ambient light sensing unavailable: {exc}")
            return

        try:
            if PIXEL_PIN == board.CAMERA_XCLK:
                print(
                    "Ambient light sensing disabled: pixel pin conflicts with camera XCLK "
                    f"({PIXEL_PIN})"
                )
                return
            camera_i2c = busio.I2C(board.CAMERA_SIOC, board.CAMERA_SIOD)
            if isinstance(board.CAMERA_DATA, tuple):
                data_pins = board.CAMERA_DATA
            else:
                data_pins = (
                    board.CAMERA_DATA,
                    board.CAMERA_DATA2,
                    board.CAMERA_DATA3,
                    board.CAMERA_DATA4,
                    board.CAMERA_DATA5,
                    board.CAMERA_DATA6,
                    board.CAMERA_DATA7,
                    board.CAMERA_DATA8,
                )
            self.camera = espcamera.Camera(
                data_pins=data_pins,
                pixel_clock_pin=board.CAMERA_PCLK,
                vsync_pin=board.CAMERA_VSYNC,
                href_pin=board.CAMERA_HREF,
                i2c=camera_i2c,
                external_clock_pin=board.CAMERA_XCLK,
                powerdown_pin=board.CAMERA_PWDN,
                pixel_format=espcamera.PixelFormat.GRAYSCALE,
                frame_size=espcamera.FrameSize.R96X96,
                framebuffer_count=1,
                grab_mode=espcamera.GrabMode.WHEN_EMPTY,
            )
            self._disable_auto_camera_adjustments()
            self._initialize_manual_camera_settings()
            self._apply_camera_settings()
            print("Ambient light sensing enabled with on-board camera")
        except Exception as exc:
            print(f"Ambient light sensing init failed: {exc}")
            self.camera = None

    def _load_settings(self):
        settings = dict(DEFAULT_AMBIENT_SETTINGS)
        try:
            with open(self.settings_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                settings.update(
                    {
                        key: loaded[key]
                        for key in DEFAULT_AMBIENT_SETTINGS
                        if key in loaded
                    }
                )
            else:
                self._save_settings(settings)
        except OSError:
            self._save_settings(settings)
        except ValueError:
            self._save_settings(settings)
        return settings

    def _save_settings(self, settings):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f)

    def _initialize_manual_camera_settings(self):
        changed = False
        if self.settings["manual_agc_gain"] is None:
            self.settings["manual_agc_gain"] = self.camera.agc_gain
            changed = True
        if self.settings["manual_aec_value"] is None:
            self.settings["manual_aec_value"] = self.camera.aec_value
            changed = True
        if changed and self.persisted_settings == DEFAULT_AMBIENT_SETTINGS:
            self.commit_settings()

    def _disable_auto_camera_adjustments(self):
        self.camera.whitebal = False
        self.camera.awb_gain = False
        self.camera.gain_ctrl = False
        self.camera.exposure_ctrl = False
        self.camera.aec2 = False
        print(
            "Ambient light camera auto controls disabled: "
            "whitebal, awb_gain, gain_ctrl, exposure_ctrl, aec2"
        )

    def _apply_camera_settings(self):
        if self.camera is None:
            return
        self.camera.agc_gain = int(self.settings["manual_agc_gain"])
        self.camera.aec_value = int(self.settings["manual_aec_value"])

    def _clamp_settings(self):
        self.settings["min_pixel_brightness"] = max(
            0.0, min(1.0, float(self.settings["min_pixel_brightness"]))
        )
        self.settings["max_pixel_brightness"] = max(
            self.settings["min_pixel_brightness"],
            min(1.0, float(self.settings["max_pixel_brightness"])),
        )
        self.settings["sample_interval_s"] = max(
            0.1, float(self.settings["sample_interval_s"])
        )
        self.settings["brightness_smoothing"] = max(
            0.0, min(1.0, float(self.settings["brightness_smoothing"]))
        )
        self.settings["dark_luma"] = max(0.0, min(1.0, float(self.settings["dark_luma"])))
        self.settings["bright_luma"] = max(
            self.settings["dark_luma"],
            min(1.0, float(self.settings["bright_luma"])),
        )
        if self.settings["manual_agc_gain"] is not None:
            self.settings["manual_agc_gain"] = int(self.settings["manual_agc_gain"])
        if self.settings["manual_aec_value"] is not None:
            self.settings["manual_aec_value"] = int(self.settings["manual_aec_value"])

    def _capture_average_luma(self):
        frame = self.camera.take(1)
        if frame is None:
            return None

        width = self.camera.width
        height = self.camera.height
        step_x = max(1, width // 8)
        step_y = max(1, height // 8)

        total = 0
        count = 0
        for y in range(0, height, step_y):
            for x in range(0, width, step_x):
                total += frame[(y * width) + x]
                count += 1

        if count == 0:
            return None
        return total / (count * 255)

    def capture_luma(self):
        if self.camera is None:
            raise RuntimeError("Ambient light camera is unavailable")
        luma = self._capture_average_luma()
        if luma is None:
            raise RuntimeError("Ambient light capture timed out")
        self.last_luma = luma
        return luma

    def get_status(self):
        return {
            "enabled": self.camera is not None,
            "settings_path": self.settings_path,
            "settings": dict(self.settings),
            "persisted_settings": dict(self.persisted_settings),
            "dirty": self.settings != self.persisted_settings,
            "last_luma": self.last_luma,
            "last_target_brightness": self.last_target_brightness,
            "last_applied_brightness": self.last_applied_brightness,
        }

    def update_settings(self, data):
        for key in DEFAULT_AMBIENT_SETTINGS:
            if key in data:
                self.settings[key] = data[key]
        self._clamp_settings()
        if self.camera is not None:
            self._apply_camera_settings()
        return self.get_status()

    def commit_settings(self):
        self._clamp_settings()
        self._save_settings(self.settings)
        self.persisted_settings = dict(self.settings)
        return self.get_status()

    def reset_settings(self):
        self.settings = dict(DEFAULT_AMBIENT_SETTINGS)
        if self.camera is not None:
            self._initialize_manual_camera_settings()
            self._apply_camera_settings()
        self._clamp_settings()
        self._save_settings(self.settings)
        self.persisted_settings = dict(self.settings)
        return self.get_status()

    def sample(self, role=None, brightness=None, luma=None):
        if luma is None:
            luma = self.capture_luma()
        else:
            luma = max(0.0, min(1.0, float(luma)))
            self.last_luma = luma

        if role == "dark":
            self.settings["dark_luma"] = luma
            if brightness is not None:
                self.settings["min_pixel_brightness"] = brightness
        elif role == "bright":
            self.settings["bright_luma"] = luma
            if brightness is not None:
                self.settings["max_pixel_brightness"] = brightness
        elif role not in (None, ""):
            raise ValueError("role must be 'dark', 'bright', or omitted")

        self._clamp_settings()
        status = self.get_status()
        status["sample"] = {
            "role": role,
            "luma": luma,
        }
        return status

    def update(self):
        if self.camera is None:
            return

        now = time.monotonic()
        if (now - self.last_sample_monotonic) < self.settings["sample_interval_s"]:
            return
        self.last_sample_monotonic = now

        try:
            luma = self._capture_average_luma()
        except Exception as exc:
            print(f"Ambient light capture failed: {exc}")
            return

        if luma is None:
            print("Ambient light capture timed out")
            return

        self.last_luma = luma
        span = max(
            0.001, self.settings["bright_luma"] - self.settings["dark_luma"]
        )
        normalized_luma = max(
            0.0, min(1.0, (luma - self.settings["dark_luma"]) / span)
        )
        target = self.settings["min_pixel_brightness"] + (
            (self.settings["max_pixel_brightness"] - self.settings["min_pixel_brightness"])
            * normalized_luma
        )
        current = self.pixels.brightness
        brightness = current + (
            (target - current) * self.settings["brightness_smoothing"]
        )
        self.pixels.brightness = brightness
        self.last_target_brightness = target
        self.last_applied_brightness = brightness
        print(
            "Ambient light level=" +
            f"{luma:.3f} target_brightness={target:.3f} " +
            f"applied_brightness={brightness:.3f}"
        )


class ClockHost:
    def __init__(self, platform, default_core_cls, ambient_light_controller=None):
        self.platform = platform
        self.default_core_cls = default_core_cls
        self.core = default_core_cls(platform)
        self.core_source = None
        self.http_server = None
        self.mdns_server = None
        self.last_sync_hour = -1
        self.consecutive_ntp_failures = 0
        self.start_monotonic = time.monotonic()
        self.ambient_light_controller = ambient_light_controller

    def rainbow_animation_frame(self, offset):
        for i in range(self.platform.pixel_count):
            pixel_index = (i * 256 // self.platform.pixel_count) + offset
            self.platform.set_pixel(i, wheel(pixel_index & 255))
        self.platform.show()

    def install_core_class(self, core_cls, source=None):
        new_core = core_cls(self.platform, config=self.core.config)
        self.core = new_core
        self.core_source = source
        print("Active animation core updated without restarting HTTP server")
        return self.core.get_state()

    def reset_core(self):
        return self.install_core_class(self.default_core_cls, source=None)

    def get_state(self):
        state = self.core.get_state()
        state["source"] = "builtin" if self.core_source is None else "uploaded"
        state["uptime_s"] = self.get_uptime_s()
        if self.ambient_light_controller is not None:
            state["ambient_light"] = self.ambient_light_controller.get_status()
        return state

    def get_uptime_s(self):
        return time.monotonic() - self.start_monotonic

    def dashboard(self):
        return {
            "mode": "device",
            "target_url": None,
            "ping": {
                "ip": str(wifi.radio.ipv4_address),
                "name": self.core.NAME,
                "version": self.core.VERSION,
                "uptime_s": self.get_uptime_s(),
            },
            "state": self.get_state(),
            "uptime": {"uptime_s": self.get_uptime_s()},
            "ambient": (
                self.ambient_light_controller.get_status()
                if self.ambient_light_controller is not None
                else None
            ),
            "pixels": None,
            "history": [],
        }

    def ui_file_response(self, request, filename, content_type):
        headers = {}
        if filename.endswith(".gz"):
            headers["Content-Encoding"] = "gzip"
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif filename.endswith(".html"):
            headers["Cache-Control"] = "no-cache"
        return FileResponse(
            request,
            filename=filename,
            root_path=UI_ROOT,
            content_type=content_type,
            headers=headers,
        )

    def tick(self):
        current_time = self.platform.now()
        if current_time.tm_hour != self.last_sync_hour:
            try:
                sync_time()
                self.consecutive_ntp_failures = 0
            except Exception as exc:
                self.consecutive_ntp_failures += 1
                self.platform.logger(f"Background NTP sync failed: {exc}")
                self.platform.logger(
                    "Consecutive NTP sync failures: "
                    f"{self.consecutive_ntp_failures}/{MAX_CONSECUTIVE_NTP_FAILURES}"
                )
                if (
                    self.consecutive_ntp_failures >= MAX_CONSECUTIVE_NTP_FAILURES
                    and 0 <= current_time.tm_hour < 5
                ):
                    self.platform.logger(
                        "NTP sync failed 5 times in a row during the overnight "
                        "maintenance window; resetting microcontroller"
                    )
                    microcontroller.reset()
            self.last_sync_hour = current_time.tm_hour
            current_time = self.platform.now()

        self.poll_http()
        if self.ambient_light_controller is not None:
            self.ambient_light_controller.update()
        self.core.tick(current_time=current_time)

    def make_webserver(self, socket_pool):
        server = adafruit_httpserver.Server(socket_pool, debug=True)

        @server.route("/")
        @server.route("/index.html")
        def ui_index(request):
            print(f"HTTP GET {request.path}")
            return self.ui_file_response(
                request, "index.html", "text/html; charset=utf-8"
            )

        @server.route("/assets/vendor/pico.min.css.gz")
        def ui_pico_css(request):
            print(f"HTTP GET {request.path}")
            return self.ui_file_response(
                request,
                "assets/vendor/pico.min.css.gz",
                "text/css; charset=utf-8",
            )

        @server.route("/assets/vendor/chart.umd.min.js.gz")
        def ui_chart_js(request):
            print(f"HTTP GET {request.path}")
            return self.ui_file_response(
                request,
                "assets/vendor/chart.umd.min.js.gz",
                "application/javascript; charset=utf-8",
            )

        @server.route("/licenses/pico-MIT.txt")
        @server.route("/licenses/chartjs-MIT.txt")
        @server.route("/THIRD_PARTY_NOTICES.md")
        def ui_license_files(request):
            print(f"HTTP GET {request.path}")
            path = request.path.lstrip("/")
            if path == "THIRD_PARTY_NOTICES.md":
                return self.ui_file_response(
                    request, "THIRD_PARTY_NOTICES.md", "text/plain; charset=utf-8"
                )
            return self.ui_file_response(request, path, "text/plain; charset=utf-8")

        @server.route("/animation/ping")
        def ping(request):
            print("HTTP GET /animation/ping")
            return adafruit_httpserver.JSONResponse(
                request,
                {
                    "ip": str(wifi.radio.ipv4_address),
                    "name": self.core.NAME,
                    "version": self.core.VERSION,
                    "uptime_s": self.get_uptime_s(),
                },
            )

        @server.route("/api/dashboard")
        def api_dashboard(request):
            print("HTTP GET /api/dashboard")
            return adafruit_httpserver.JSONResponse(request, self.dashboard())

        @server.route("/system/uptime")
        @server.route("/api/system/uptime")
        def system_uptime(request):
            print(f"HTTP GET {request.path}")
            return adafruit_httpserver.JSONResponse(
                request,
                {"uptime_s": self.get_uptime_s()},
            )

        @server.route("/animation/state")
        @server.route("/api/animation/state")
        def animation_state(request):
            print(f"HTTP GET {request.path}")
            return adafruit_httpserver.JSONResponse(request, self.get_state())

        @server.route("/animation/config", [PUT, POST])
        @server.route("/api/animation/config", [PUT, POST])
        def animation_config(request):
            data = request.json()
            print(f"HTTP {request.method} {request.path} payload={data}")
            if not isinstance(data, dict):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "Expected JSON object"}
                )
            try:
                state = self.core.update_config(data)
            except (TypeError, ValueError) as exc:
                return adafruit_httpserver.JSONResponse(request, {"error": str(exc)})
            return adafruit_httpserver.JSONResponse(request, state)

        @server.route("/animation/install", [POST, PUT])
        @server.route("/api/animation/install", [POST, PUT])
        def animation_install(request):
            data = request.json()
            print(f"HTTP {request.method} {request.path}")
            if not isinstance(data, dict) or "source" not in data:
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "Expected JSON object with source"}
                )
            source = data["source"]
            if not isinstance(source, str):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "source must be a string"}
                )
            try:
                print(f"Installing uploaded core source ({len(source)} bytes)")
                core_cls = load_core_class_from_source(source)
                state = self.install_core_class(core_cls, source=source)
                print(f"Installed core {state['name']} v{state['version']}")
            except Exception as exc:
                print(f"Install failed: {exc}")
                return adafruit_httpserver.JSONResponse(
                    request, {"error": f"Install failed: {exc}"}
                )
            return adafruit_httpserver.JSONResponse(request, {"ok": True, "state": state})

        @server.route("/animation/reset", [POST, PUT])
        @server.route("/api/animation/reset", [POST, PUT])
        def animation_reset(request):
            print(f"HTTP {request.method} {request.path}")
            state = self.reset_core()
            print(f"Reset to core {state['name']} v{state['version']}")
            return adafruit_httpserver.JSONResponse(
                request, {"ok": True, "reset": True, "state": state}
            )

        @server.route("/animation/core", [POST, PUT])
        @server.route("/api/animation/core", [POST, PUT])
        def animation_core(request):
            data = request.json()
            print(f"HTTP {request.method} {request.path} payload={data}")
            if not isinstance(data, dict):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "Expected JSON object"}
                )
            path = data.get("path", "")
            payload = data.get("data", {})
            if not isinstance(path, str):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "path must be a string"}
                )
            if not isinstance(payload, dict):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "data must be a JSON object"}
                )
            try:
                response = self.core.handle_api(path, payload)
                print(f"Core API path={path} response={response}")
            except Exception as exc:
                print(f"Core API failed for path={path}: {exc}")
                return adafruit_httpserver.JSONResponse(request, {"error": str(exc)})
            return adafruit_httpserver.JSONResponse(request, response)

        @server.route("/ambient/state")
        @server.route("/api/ambient/state")
        def ambient_state(request):
            print(f"HTTP GET {request.path}")
            if self.ambient_light_controller is None:
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "Ambient light controller unavailable"}
                )
            return adafruit_httpserver.JSONResponse(
                request, self.ambient_light_controller.get_status()
            )

        @server.route("/ambient/config", [PUT, POST])
        @server.route("/api/ambient/config", [PUT, POST])
        def ambient_config(request):
            data = request.json()
            print(f"HTTP {request.method} {request.path} payload={data}")
            if not isinstance(data, dict):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "Expected JSON object"}
                )
            try:
                status = self.ambient_light_controller.update_settings(data)
            except Exception as exc:
                return adafruit_httpserver.JSONResponse(request, {"error": str(exc)})
            return adafruit_httpserver.JSONResponse(request, status)

        @server.route("/ambient/sample", [POST, PUT])
        @server.route("/api/ambient/sample", [POST, PUT])
        def ambient_sample(request):
            data = request.json()
            print(f"HTTP {request.method} {request.path} payload={data}")
            if data is None:
                data = {}
            if not isinstance(data, dict):
                return adafruit_httpserver.JSONResponse(
                    request, {"error": "Expected JSON object"}
                )
            role = data.get("role")
            brightness = data.get("brightness")
            luma = data.get("luma")
            try:
                status = self.ambient_light_controller.sample(
                    role=role, brightness=brightness, luma=luma
                )
            except Exception as exc:
                return adafruit_httpserver.JSONResponse(request, {"error": str(exc)})
            return adafruit_httpserver.JSONResponse(request, status)

        @server.route("/ambient/commit", [POST, PUT])
        @server.route("/api/ambient/commit", [POST, PUT])
        def ambient_commit(request):
            print(f"HTTP {request.method} {request.path}")
            try:
                status = self.ambient_light_controller.commit_settings()
            except Exception as exc:
                return adafruit_httpserver.JSONResponse(request, {"error": str(exc)})
            return adafruit_httpserver.JSONResponse(request, status)

        @server.route("/ambient/reset", [POST, PUT])
        @server.route("/api/ambient/reset", [POST, PUT])
        def ambient_reset(request):
            print(f"HTTP {request.method} {request.path}")
            try:
                status = self.ambient_light_controller.reset_settings()
            except Exception as exc:
                return adafruit_httpserver.JSONResponse(request, {"error": str(exc)})
            return adafruit_httpserver.JSONResponse(request, status)

        base_url = f"http://{wifi.radio.ipv4_address}:{SERVER_PORT}"
        print(f"Starting HTTP server at {base_url}")
        print(f"  {base_url}/")
        print(f"  {base_url}/animation/ping")
        print(f"  {base_url}/animation/state")
        print(f"  {base_url}/system/uptime")
        print(f"  {base_url}/api/dashboard")
        print(f"  {base_url}/animation/config")
        print(f"  {base_url}/animation/install")
        print(f"  {base_url}/animation/reset")
        print(f"  {base_url}/animation/core")
        print(f"  {base_url}/ambient/state")
        print(f"  {base_url}/ambient/config")
        print(f"  {base_url}/ambient/sample")
        print(f"  {base_url}/ambient/commit")
        print(f"  {base_url}/ambient/reset")
        server.start(port=SERVER_PORT)
        self.http_server = server
        return server

    def start_http(self):
        socket_pool = socketpool.SocketPool(wifi.radio)
        self.make_webserver(socket_pool)

    def poll_http(self):
        if self.http_server is None:
            return

        self.http_server.poll()


def main():
    raw_pixels = neopixel.NeoPixel(
        PIXEL_PIN, PIXEL_COUNT, brightness=BRIGHTNESS, auto_write=False
    )
    pixels = PixelOffsetWrapper(raw_pixels, PIXEL_INDEX_OFFSET)
    platform = ClockPlatform(pixels)
    ambient_light_controller = AmbientLightController(pixels)
    host = ClockHost(
        platform,
        ClockAnimationCore,
        ambient_light_controller=ambient_light_controller,
    )

    host.rainbow_animation_frame(0)
    connect_wifi()
    host.mdns_server = start_mdns()
    host.start_http()

    print("Starting clock display...")
    while True:
        host.tick()


main()
