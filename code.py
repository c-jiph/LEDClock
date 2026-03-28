import time

import adafruit_httpserver
import adafruit_ntp
import board
import neopixel
import rtc
import socketpool
import sys
import wifi
from adafruit_httpserver import POST, PUT

from animation import ClockAnimationCore, ClockPlatform, load_core_class_from_source, wheel

PIXEL_PIN = board.IO16 if "FooToy" in sys.implementation._machine else board.IO0
PIXEL_COUNT = 60
BRIGHTNESS = 0.2
SERVER_PORT = 81

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


class ClockHost:
    def __init__(self, platform, default_core_cls):
        self.platform = platform
        self.default_core_cls = default_core_cls
        self.core = default_core_cls(platform)
        self.core_source = None
        self.http_server = None
        self.last_sync_hour = -1

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
        return state

    def tick(self):
        current_time = self.platform.now()
        if current_time.tm_hour != self.last_sync_hour:
            try:
                sync_time()
            except Exception as exc:
                self.platform.logger(f"Background NTP sync failed: {exc}")
            self.last_sync_hour = current_time.tm_hour
            current_time = self.platform.now()

        self.poll_http()
        self.core.tick(current_time=current_time)

    def make_webserver(self, socket_pool):
        server = adafruit_httpserver.Server(socket_pool, debug=True)

        @server.route("/animation/ping")
        def ping(request):
            print("HTTP GET /animation/ping")
            return adafruit_httpserver.JSONResponse(
                request,
                {
                    "ip": str(wifi.radio.ipv4_address),
                    "name": self.core.NAME,
                    "version": self.core.VERSION,
                },
            )

        @server.route("/animation/state")
        def animation_state(request):
            print("HTTP GET /animation/state")
            return adafruit_httpserver.JSONResponse(request, self.get_state())

        @server.route("/animation/config", [PUT, POST])
        def animation_config(request):
            data = request.json()
            print(f"HTTP {request.method} /animation/config payload={data}")
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
        def animation_install(request):
            data = request.json()
            print(f"HTTP {request.method} /animation/install")
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
        def animation_reset(request):
            print(f"HTTP {request.method} /animation/reset")
            state = self.reset_core()
            print(f"Reset to core {state['name']} v{state['version']}")
            return adafruit_httpserver.JSONResponse(
                request, {"ok": True, "reset": True, "state": state}
            )

        @server.route("/animation/core", [POST, PUT])
        def animation_core(request):
            data = request.json()
            print(f"HTTP {request.method} /animation/core payload={data}")
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

        base_url = f"http://{wifi.radio.ipv4_address}:{SERVER_PORT}"
        print(f"Starting HTTP server at {base_url}")
        print(f"  {base_url}/animation/ping")
        print(f"  {base_url}/animation/state")
        print(f"  {base_url}/animation/config")
        print(f"  {base_url}/animation/install")
        print(f"  {base_url}/animation/reset")
        print(f"  {base_url}/animation/core")
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
    pixels = neopixel.NeoPixel(
        PIXEL_PIN, PIXEL_COUNT, brightness=BRIGHTNESS, auto_write=False
    )
    platform = ClockPlatform(pixels)
    host = ClockHost(platform, ClockAnimationCore)

    host.rainbow_animation_frame(0)
    connect_wifi()
    host.start_http()

    print("Starting clock display...")
    while True:
        host.tick()


main()
