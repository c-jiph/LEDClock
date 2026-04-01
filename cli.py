import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from animation import ClockAnimationCore, ClockPlatform, load_core_class_from_source

ANSI_RESET = "\x1b[0m"
ANSI_CLEAR = "\x1b[2J\x1b[H"
ANSI_HIDE_CURSOR = "\x1b[?25l"
ANSI_SHOW_CURSOR = "\x1b[?25h"
DEFAULT_URL = "http://127.0.0.1:8000"
UI_ROOT = os.path.join(os.path.dirname(__file__), "ui")
DEFAULT_AMBIENT_SETTINGS = {
    "dark_luma": 0.0,
    "bright_luma": 1.0,
    "min_pixel_brightness": 0.03,
    "max_pixel_brightness": 0.8,
    "sample_interval_s": 5.0,
    "brightness_smoothing": 0.25,
    "manual_agc_gain": None,
    "manual_aec_value": None,
}


class MemoryPixels:
    def __init__(self, count):
        self._pixels = [(0, 0, 0)] * count
        self.brightness = 0.2

    def __len__(self):
        return len(self._pixels)

    def __getitem__(self, index):
        return self._pixels[index]

    def __setitem__(self, index, value):
        self._pixels[index] = tuple(value)

    def show(self):
        pass

    def snapshot(self):
        return {
            "count": len(self._pixels),
            "brightness": self.brightness,
            "pixels": [list(pixel) for pixel in self._pixels],
        }


class SimulatorAmbientLightController:
    def __init__(self, pixels, settings_path):
        self.pixels = pixels
        self.settings_path = settings_path
        self.settings = self._load_settings()
        self.persisted_settings = dict(self.settings)
        self.last_luma = None
        self.last_target_brightness = pixels.brightness
        self.last_applied_brightness = pixels.brightness
        self.last_sample_monotonic = -self.settings["sample_interval_s"]

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

    def _apply_luma(self, luma):
        span = max(0.001, self.settings["bright_luma"] - self.settings["dark_luma"])
        normalized_luma = max(
            0.0, min(1.0, (luma - self.settings["dark_luma"]) / span)
        )
        target = self.settings["min_pixel_brightness"] + (
            (self.settings["max_pixel_brightness"] - self.settings["min_pixel_brightness"])
            * normalized_luma
        )
        brightness = self.pixels.brightness + (
            (target - self.pixels.brightness) * self.settings["brightness_smoothing"]
        )
        self.pixels.brightness = brightness
        self.last_luma = luma
        self.last_target_brightness = target
        self.last_applied_brightness = brightness

    def get_status(self):
        return {
            "enabled": True,
            "simulated": True,
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
        if self.last_luma is not None:
            self._apply_luma(self.last_luma)
        return self.get_status()

    def commit_settings(self):
        self._clamp_settings()
        self._save_settings(self.settings)
        self.persisted_settings = dict(self.settings)
        return self.get_status()

    def reset_settings(self):
        self.settings = dict(DEFAULT_AMBIENT_SETTINGS)
        self._clamp_settings()
        self._save_settings(self.settings)
        self.persisted_settings = dict(self.settings)
        self.pixels.brightness = self.settings["min_pixel_brightness"]
        self.last_target_brightness = self.pixels.brightness
        self.last_applied_brightness = self.pixels.brightness
        return self.get_status()

    def sample(self, role=None, brightness=None, luma=None):
        if luma is None:
            if self.last_luma is None:
                raise ValueError("luma is required for simulator sampling")
            luma = self.last_luma
        else:
            luma = max(0.0, min(1.0, float(luma)))
            self._apply_luma(luma)

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
        if self.last_luma is not None:
            self._apply_luma(self.last_luma)
        status = self.get_status()
        status["sample"] = {"role": role, "luma": luma}
        return status


class SimulatorHost:
    def __init__(self, platform, default_core_cls, ambient_light_controller):
        self.platform = platform
        self.default_core_cls = default_core_cls
        self.core = default_core_cls(platform)
        self.core_source = None
        self.ambient_light_controller = ambient_light_controller
        self._lock = threading.RLock()
        self.start_monotonic = time.monotonic()

    def uptime_s(self):
        return time.monotonic() - self.start_monotonic

    def get_state(self):
        with self._lock:
            state = self.core.get_state()
            state["source"] = "builtin" if self.core_source is None else "uploaded"
            state["uptime_s"] = self.uptime_s()
            state["ambient_light"] = self.ambient_light_controller.get_status()
            return state

    def install_source(self, source):
        with self._lock:
            core_cls = load_core_class_from_source(source)
            self.core = core_cls(self.platform, config=self.core.config)
            self.core_source = source
            return self.get_state()

    def reset_core(self):
        with self._lock:
            self.core = self.default_core_cls(self.platform, config=self.core.config)
            self.core_source = None
            return self.get_state()

    def update_config(self, config):
        with self._lock:
            return self.core.update_config(config)

    def tick(self):
        with self._lock:
            if self.ambient_light_controller.last_luma is not None:
                self.ambient_light_controller._apply_luma(
                    self.ambient_light_controller.last_luma
                )
            self.core.tick()

    def frame_delay(self):
        with self._lock:
            return self.core.config["frame_delay_s"]

    def render(self):
        with self._lock:
            label = f"{self.core.NAME} v{self.core.VERSION}"
            return render_ring(self.platform.pixels, label=label)

    def pixel_state(self):
        with self._lock:
            return self.platform.pixels.snapshot()

    def ping(self):
        with self._lock:
            return {
                "ip": "127.0.0.1",
                "name": self.core.NAME,
                "version": self.core.VERSION,
                "uptime_s": self.uptime_s(),
            }

    def uptime(self):
        with self._lock:
            return {"uptime_s": self.uptime_s()}

    def handle_core_api(self, path, data):
        with self._lock:
            return self.core.handle_api(path, data)

    def ambient_state(self):
        with self._lock:
            return self.ambient_light_controller.get_status()

    def ambient_update(self, data):
        with self._lock:
            return self.ambient_light_controller.update_settings(data)

    def ambient_commit(self):
        with self._lock:
            return self.ambient_light_controller.commit_settings()

    def ambient_reset(self):
        with self._lock:
            return self.ambient_light_controller.reset_settings()

    def ambient_sample(self, role=None, brightness=None, luma=None):
        with self._lock:
            return self.ambient_light_controller.sample(
                role=role, brightness=brightness, luma=luma
            )


class SimulatorRuntime:
    def __init__(self, host):
        self.host = host
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self):
        while not self._stop.is_set():
            self.host.tick()
            self._stop.wait(self.host.frame_delay())


class UIBackend:
    def __init__(self, mode, target_url=None, host=None):
        self.mode = mode
        self.target_url = target_url.rstrip("/") if target_url else None
        self.host = host
        self._history = deque(maxlen=180)
        self._history_lock = threading.Lock()
        self._last_history_sample = 0.0

    def _record_history(self, ambient_state):
        if not isinstance(ambient_state, dict):
            return
        now = time.time()
        if self._last_history_sample and (now - self._last_history_sample) < 4.5:
            return
        self._last_history_sample = now
        entry = {
            "ts": now,
            "last_luma": ambient_state.get("last_luma"),
            "last_target_brightness": ambient_state.get("last_target_brightness"),
            "last_applied_brightness": ambient_state.get("last_applied_brightness"),
        }
        with self._history_lock:
            self._history.append(entry)

    def _history_snapshot(self):
        with self._history_lock:
            return list(self._history)

    def _http(self, path, method="GET", payload=None):
        return http_json(with_base_url(self.target_url, path), method=method, payload=payload)

    def ping(self):
        if self.mode == "simulation":
            return self.host.ping()
        return self._http("/animation/ping")

    def state(self):
        if self.mode == "simulation":
            return self.host.get_state()
        return self._http("/animation/state")

    def uptime(self):
        if self.mode == "simulation":
            return self.host.uptime()
        return self._http("/system/uptime")

    def animation_config(self, payload):
        if self.mode == "simulation":
            return self.host.update_config(payload)
        return self._http("/animation/config", method="PUT", payload=payload)

    def animation_install(self, source):
        if self.mode == "simulation":
            return {"ok": True, "state": self.host.install_source(source)}
        return self._http("/animation/install", method="POST", payload={"source": source})

    def animation_reset(self):
        if self.mode == "simulation":
            return {"ok": True, "reset": True, "state": self.host.reset_core()}
        return self._http("/animation/reset", method="POST", payload={})

    def core_request(self, path, data):
        if self.mode == "simulation":
            return self.host.handle_core_api(path, data)
        return self._http(
            "/animation/core",
            method="POST",
            payload={"path": path, "data": data},
        )

    def ambient_state(self):
        if self.mode == "simulation":
            state = self.host.ambient_state()
        else:
            state = self._http("/ambient/state")
        self._record_history(state)
        return state

    def ambient_config(self, payload):
        if self.mode == "simulation":
            state = self.host.ambient_update(payload)
        else:
            state = self._http("/ambient/config", method="PUT", payload=payload)
        self._record_history(state)
        return state

    def ambient_sample(self, payload):
        if self.mode == "simulation":
            state = self.host.ambient_sample(
                role=payload.get("role"),
                brightness=payload.get("brightness"),
                luma=payload.get("luma"),
            )
        else:
            state = self._http("/ambient/sample", method="POST", payload=payload)
        self._record_history(state)
        return state

    def ambient_commit(self):
        if self.mode == "simulation":
            state = self.host.ambient_commit()
        else:
            state = self._http("/ambient/commit", method="POST", payload={})
        self._record_history(state)
        return state

    def ambient_reset(self):
        if self.mode == "simulation":
            state = self.host.ambient_reset()
        else:
            state = self._http("/ambient/reset", method="POST", payload={})
        self._record_history(state)
        return state

    def pixel_state(self):
        if self.mode != "simulation":
            return None
        return self.host.pixel_state()

    def dashboard(self):
        ping = self.ping()
        state = self.state()
        ambient = state.get("ambient_light")
        if not isinstance(ambient, dict):
            ambient = self.ambient_state()
        else:
            self._record_history(ambient)
        return {
            "mode": self.mode,
            "target_url": self.target_url,
            "ping": ping,
            "state": state,
            "uptime": self.uptime(),
            "ambient": ambient,
            "pixels": self.pixel_state(),
            "history": self._history_snapshot(),
        }


def fg(color, brightness=1.0, text="●"):
    r, g, b = color
    r = int(r * brightness)
    g = int(g * brightness)
    b = int(b * brightness)
    if (r, g, b) == (0, 0, 0):
        r, g, b = (22, 22, 22)
    return f"\x1b[38;2;{r};{g};{b}m{text}"


def render_ring(pixels, label=None):
    rows = 23
    cols = 39
    grid = [[" " for _ in range(cols)] for _ in range(rows)]
    center_y = rows // 2
    center_x = cols // 2
    radius_y = 9.5
    radius_x = 15.0

    brightness = getattr(pixels, "brightness", 1.0)
    for index, color in enumerate(pixels):
        angle = ((index / len(pixels)) * (2 * math.pi)) - (math.pi / 2)
        y = int(round(center_y + math.sin(angle) * radius_y))
        x = int(round(center_x + math.cos(angle) * radius_x))
        grid[y][x] = fg(color, brightness=brightness)

    if label:
        start = max(0, center_x - (len(label) // 2))
        for offset, char in enumerate(label[: cols - start]):
            grid[center_y][start + offset] = f"\x1b[38;2;180;180;180m{char}"

    lines = ["".join(row) + ANSI_RESET for row in grid]
    return ANSI_CLEAR + "\n".join(lines) + ANSI_RESET


def load_core(platform, source_path=None):
    if source_path is None:
        return ClockAnimationCore(platform)
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()
    core_cls = load_core_class_from_source(source)
    return core_cls(platform)


def apply_optional_config(core, args):
    config = {}
    if getattr(args, "fade_step", None) is not None:
        config["fade_step"] = args.fade_step
    if getattr(args, "frame_delay", None) is not None:
        config["frame_delay_s"] = args.frame_delay
    if config:
        core.update_config(config)


def http_json(url, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode("utf-8")
        if not body:
            return None
        return json.loads(body)


def print_json(data):
    print(json.dumps(data, indent=2, sort_keys=True))


def make_handler(host):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def log_message(self, fmt, *args):
            return

        def _dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                if path == "/animation/ping" and method == "GET":
                    self._send_json(200, host.ping())
                    return
                if path == "/animation/state" and method == "GET":
                    self._send_json(200, host.get_state())
                    return
                if path == "/system/uptime" and method == "GET":
                    self._send_json(200, host.uptime())
                    return
                if path == "/animation/config" and method in ("POST", "PUT"):
                    payload = self._read_json()
                    if not isinstance(payload, dict):
                        self._send_json(400, {"error": "Expected JSON object"})
                        return
                    self._send_json(200, host.update_config(payload))
                    return
                if path == "/animation/install" and method in ("POST", "PUT"):
                    payload = self._read_json()
                    if not isinstance(payload, dict) or "source" not in payload:
                        self._send_json(400, {"error": "Expected JSON object with source"})
                        return
                    if not isinstance(payload["source"], str):
                        self._send_json(400, {"error": "source must be a string"})
                        return
                    self._send_json(
                        200, {"ok": True, "state": host.install_source(payload["source"])}
                    )
                    return
                if path == "/animation/reset" and method in ("POST", "PUT"):
                    self._send_json(200, {"ok": True, "reset": True, "state": host.reset_core()})
                    return
                if path == "/animation/core" and method in ("POST", "PUT"):
                    payload = self._read_json()
                    if not isinstance(payload, dict):
                        self._send_json(400, {"error": "Expected JSON object"})
                        return
                    api_path = payload.get("path", "")
                    api_data = payload.get("data", {})
                    if not isinstance(api_path, str):
                        self._send_json(400, {"error": "path must be a string"})
                        return
                    if not isinstance(api_data, dict):
                        self._send_json(400, {"error": "data must be a JSON object"})
                        return
                    self._send_json(200, host.handle_core_api(api_path, api_data))
                    return
                if path == "/ambient/state" and method == "GET":
                    self._send_json(200, host.ambient_state())
                    return
                if path == "/ambient/config" and method in ("POST", "PUT"):
                    payload = self._read_json()
                    if not isinstance(payload, dict):
                        self._send_json(400, {"error": "Expected JSON object"})
                        return
                    self._send_json(200, host.ambient_update(payload))
                    return
                if path == "/ambient/sample" and method in ("POST", "PUT"):
                    payload = self._read_json()
                    if payload is None:
                        payload = {}
                    if not isinstance(payload, dict):
                        self._send_json(400, {"error": "Expected JSON object"})
                        return
                    self._send_json(
                        200,
                        host.ambient_sample(
                            role=payload.get("role"),
                            brightness=payload.get("brightness"),
                            luma=payload.get("luma"),
                        ),
                    )
                    return
                if path == "/ambient/commit" and method in ("POST", "PUT"):
                    self._send_json(200, host.ambient_commit())
                    return
                if path == "/ambient/reset" and method in ("POST", "PUT"):
                    self._send_json(200, host.ambient_reset())
                    return
                self._send_json(404, {"error": f"Unknown endpoint: {path}"})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return None
            body = self.rfile.read(length)
            return json.loads(body.decode("utf-8"))

        def _send_json(self, status_code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def read_ui_file(path):
    with open(path, "rb") as f:
        return f.read()


def ui_fs_path(relative_path):
    safe_parts = [part for part in relative_path.split("/") if part not in ("", ".", "..")]
    return os.path.join(UI_ROOT, *safe_parts)


def ui_content_type(relative_path):
    if relative_path.endswith(".html"):
        return "text/html; charset=utf-8"
    if relative_path.endswith(".css") or relative_path.endswith(".css.gz"):
        return "text/css; charset=utf-8"
    if relative_path.endswith(".js") or relative_path.endswith(".js.gz"):
        return "application/javascript; charset=utf-8"
    if relative_path.endswith(".txt") or relative_path.endswith(".md"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def ui_headers(relative_path):
    headers = {}
    if relative_path.endswith(".gz"):
        headers["Content-Encoding"] = "gzip"
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif relative_path.endswith(".html"):
        headers["Cache-Control"] = "no-cache"
    return headers


def make_ui_handler(backend):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def log_message(self, fmt, *args):
            return

        def _dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            try:
                if method == "GET" and path in ("/", "/index.html"):
                    self._send_bytes(
                        200,
                        read_ui_file(ui_fs_path("index.html")),
                        content_type=ui_content_type("index.html"),
                        headers=ui_headers("index.html"),
                    )
                    return
                if method == "GET" and (
                    path.startswith("/assets/")
                    or path.startswith("/licenses/")
                    or path == "/THIRD_PARTY_NOTICES.md"
                ):
                    relative_path = path.lstrip("/")
                    self._send_bytes(
                        200,
                        read_ui_file(ui_fs_path(relative_path)),
                        content_type=ui_content_type(relative_path),
                        headers=ui_headers(relative_path),
                    )
                    return
                if path == "/api/dashboard" and method == "GET":
                    self._send_json(200, backend.dashboard())
                    return
                if path == "/api/system/uptime" and method == "GET":
                    self._send_json(200, backend.uptime())
                    return
                if path == "/api/animation/config" and method in ("POST", "PUT"):
                    payload = self._require_json_object()
                    self._send_json(200, backend.animation_config(payload))
                    return
                if path == "/api/animation/install" and method in ("POST", "PUT"):
                    payload = self._require_json_object()
                    source = payload.get("source")
                    if not isinstance(source, str):
                        self._send_json(400, {"error": "source must be a string"})
                        return
                    self._send_json(200, backend.animation_install(source))
                    return
                if path == "/api/animation/reset" and method in ("POST", "PUT"):
                    self._send_json(200, backend.animation_reset())
                    return
                if path == "/api/animation/core" and method in ("POST", "PUT"):
                    payload = self._require_json_object()
                    api_path = payload.get("path", "")
                    data = payload.get("data", {})
                    if not isinstance(api_path, str):
                        self._send_json(400, {"error": "path must be a string"})
                        return
                    if not isinstance(data, dict):
                        self._send_json(400, {"error": "data must be a JSON object"})
                        return
                    self._send_json(200, backend.core_request(api_path, data))
                    return
                if path == "/api/ambient/state" and method == "GET":
                    self._send_json(200, backend.ambient_state())
                    return
                if path == "/api/ambient/config" and method in ("POST", "PUT"):
                    payload = self._require_json_object()
                    self._send_json(200, backend.ambient_config(payload))
                    return
                if path == "/api/ambient/sample" and method in ("POST", "PUT"):
                    payload = self._require_json_object(default={})
                    self._send_json(200, backend.ambient_sample(payload))
                    return
                if path == "/api/ambient/commit" and method in ("POST", "PUT"):
                    self._send_json(200, backend.ambient_commit())
                    return
                if path == "/api/ambient/reset" and method in ("POST", "PUT"):
                    self._send_json(200, backend.ambient_reset())
                    return
                self._send_json(404, {"error": f"Unknown endpoint: {path}"})
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                try:
                    payload = json.loads(body) if body else {"error": f"HTTP {exc.code}"}
                except ValueError:
                    payload = {"error": body or f"HTTP {exc.code}"}
                self._send_json(exc.code, payload)
            except FileNotFoundError:
                self._send_json(404, {"error": f"Unknown endpoint: {path}"})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})

        def _read_json(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return None
            body = self.rfile.read(length)
            return json.loads(body.decode("utf-8"))

        def _require_json_object(self, default=None):
            payload = self._read_json()
            if payload is None:
                payload = default
            if not isinstance(payload, dict):
                raise ValueError("Expected JSON object")
            return payload

        def _send_json(self, status_code, payload):
            encoded = json.dumps(payload).encode("utf-8")
            self._send_bytes(status_code, encoded, content_type="application/json")

        def _send_bytes(self, status_code, body, content_type, headers=None):
            headers = headers or {}
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for header, value in headers.items():
                self.send_header(header, value)
            self.end_headers()
            self.wfile.write(body)

    return Handler


def run_renderer(core, frames=0):
    frame = 0
    sys.stdout.write(ANSI_HIDE_CURSOR)
    try:
        while frames == 0 or frame < frames:
            core.tick()
            label = f"{core.NAME} v{core.VERSION}"
            sys.stdout.write(render_ring(core.platform.pixels, label=label))
            sys.stdout.flush()
            frame += 1
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_RESET + "\n")
        sys.stdout.flush()


def run_command(args):
    pixels = MemoryPixels(60)
    platform = ClockPlatform(pixels, time_source=time.localtime, sleeper=time.sleep)
    core = load_core(platform, args.source)
    apply_optional_config(core, args)
    run_renderer(core, frames=args.frames)


def build_simulator_host(args):
    pixels = MemoryPixels(60)
    platform = ClockPlatform(pixels, time_source=time.localtime, sleeper=lambda _: None)
    ambient = SimulatorAmbientLightController(pixels, args.ambient_settings_file)
    host = SimulatorHost(platform, ClockAnimationCore, ambient)
    if getattr(args, "source", None) is not None:
        with open(args.source, "r", encoding="utf-8") as f:
            host.install_source(f.read())
    if getattr(args, "fade_step", None) is not None or getattr(args, "frame_delay", None) is not None:
        host.update_config(
            {
                key: value
                for key, value in (
                    ("fade_step", getattr(args, "fade_step", None)),
                    ("frame_delay_s", getattr(args, "frame_delay", None)),
                )
                if value is not None
            }
        )
    return host


def serve_command(args):
    host = build_simulator_host(args)
    httpd = ThreadingHTTPServer((args.bind, args.port), make_handler(host))
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    print(f"Simulator serving on http://{args.bind}:{args.port}")

    frame = 0
    sys.stdout.write(ANSI_HIDE_CURSOR)
    try:
        while args.frames == 0 or frame < args.frames:
            host.tick()
            if not args.no_render:
                sys.stdout.write(host.render())
                sys.stdout.flush()
            time.sleep(host.frame_delay())
            frame += 1
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        sys.stdout.write(ANSI_SHOW_CURSOR + ANSI_RESET + "\n")
        sys.stdout.flush()


def ui_command(args):
    runtime = None
    if args.target_url:
        backend = UIBackend(mode="proxy", target_url=args.target_url)
    else:
        host = build_simulator_host(args)
        runtime = SimulatorRuntime(host)
        runtime.start()
        backend = UIBackend(mode="simulation", host=host)

    httpd = ThreadingHTTPServer((args.bind, args.port), make_ui_handler(backend))
    print(f"Control UI serving on http://{args.bind}:{args.port}")
    if args.target_url:
        print(f"Proxying real device at {args.target_url}")
    else:
        print("Running in local simulation mode with live pixel preview")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        if runtime is not None:
            runtime.stop()


def with_base_url(url, path):
    return urllib.parse.urljoin(url.rstrip("/") + "/", path.lstrip("/"))


def ping_command(args):
    print_json(http_json(with_base_url(args.url, "/animation/ping")))


def state_command(args):
    print_json(http_json(with_base_url(args.url, "/animation/state")))


def uptime_command(args):
    print_json(http_json(with_base_url(args.url, "/system/uptime")))


def config_command(args):
    payload = {}
    if args.fade_step is not None:
        payload["fade_step"] = args.fade_step
    if args.frame_delay is not None:
        payload["frame_delay_s"] = args.frame_delay
    if not payload:
        raise SystemExit("config requires --fade-step and/or --frame-delay")
    print_json(http_json(with_base_url(args.url, "/animation/config"), method="PUT", payload=payload))


def install_command(args):
    with open(args.source, "r", encoding="utf-8") as f:
        source = f.read()
    print_json(
        http_json(
            with_base_url(args.url, "/animation/install"),
            method="POST",
            payload={"source": source},
        )
    )


def reset_command(args):
    print_json(http_json(with_base_url(args.url, "/animation/reset"), method="POST", payload={}))


def request_command(args):
    data = {}
    if args.json is not None:
        data = json.loads(args.json)
    if not isinstance(data, dict):
        raise SystemExit("--json must decode to a JSON object")
    print_json(
        http_json(
            with_base_url(args.url, "/animation/core"),
            method="POST",
            payload={"path": args.path, "data": data},
        )
    )


def ambient_state_command(args):
    print_json(http_json(with_base_url(args.url, "/ambient/state")))


def ambient_set_command(args):
    payload = {}
    for arg_name, key in (
        ("dark_luma", "dark_luma"),
        ("bright_luma", "bright_luma"),
        ("min_brightness", "min_pixel_brightness"),
        ("max_brightness", "max_pixel_brightness"),
        ("sample_interval", "sample_interval_s"),
        ("smoothing", "brightness_smoothing"),
        ("manual_agc_gain", "manual_agc_gain"),
        ("manual_aec_value", "manual_aec_value"),
    ):
        value = getattr(args, arg_name)
        if value is not None:
            payload[key] = value
    if not payload:
        raise SystemExit("ambient-set requires at least one setting option")
    print_json(http_json(with_base_url(args.url, "/ambient/config"), method="PUT", payload=payload))


def ambient_commit_command(args):
    print_json(http_json(with_base_url(args.url, "/ambient/commit"), method="POST", payload={}))


def ambient_reset_command(args):
    print_json(http_json(with_base_url(args.url, "/ambient/reset"), method="POST", payload={}))


def ambient_sample_command(args):
    payload = {}
    if args.role is not None:
        payload["role"] = args.role
    if args.brightness is not None:
        payload["brightness"] = args.brightness
    if args.luma is not None:
        payload["luma"] = args.luma
    print_json(http_json(with_base_url(args.url, "/ambient/sample"), method="POST", payload=payload))


def add_common_url_argument(parser):
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Base URL. Default: {DEFAULT_URL}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Render Clock animation cores and manipulate animation endpoints."
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Render a core locally in the terminal.")
    run_parser.add_argument("--source", help="Python file defining AnimationCore or CORE_CLASS.")
    run_parser.add_argument("--frames", type=int, default=0, help="Number of frames. 0 = infinite.")
    run_parser.add_argument("--fade-step", type=int, help="Override fade_step config.")
    run_parser.add_argument("--frame-delay", type=float, help="Override frame_delay_s config.")
    run_parser.set_defaults(func=run_command)

    serve_parser = subparsers.add_parser(
        "serve", help="Run a local simulator with the same animation endpoints."
    )
    serve_parser.add_argument("--bind", default="127.0.0.1", help="Bind address.")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    serve_parser.add_argument("--source", help="Initial uploaded core source file.")
    serve_parser.add_argument("--frames", type=int, default=0, help="Number of frames. 0 = infinite.")
    serve_parser.add_argument("--fade-step", type=int, help="Override fade_step config.")
    serve_parser.add_argument("--frame-delay", type=float, help="Override frame_delay_s config.")
    serve_parser.add_argument(
        "--ambient-settings-file",
        default=".ambient_settings_sim.json",
        help="Simulator ambient-light settings file.",
    )
    serve_parser.add_argument("--no-render", action="store_true", help="Disable terminal rendering.")
    serve_parser.set_defaults(func=serve_command)

    ui_parser = subparsers.add_parser(
        "ui", help="Run a local browser UI for simulation or proxied device control."
    )
    ui_parser.add_argument("--bind", default="127.0.0.1", help="Bind address.")
    ui_parser.add_argument("--port", type=int, default=8080, help="Port to listen on.")
    ui_parser.add_argument(
        "--target-url",
        help="Real device base URL. If omitted, run an in-process simulation instead.",
    )
    ui_parser.add_argument("--source", help="Initial uploaded core source file for simulation mode.")
    ui_parser.add_argument("--fade-step", type=int, help="Initial fade_step for simulation mode.")
    ui_parser.add_argument("--frame-delay", type=float, help="Initial frame_delay_s for simulation mode.")
    ui_parser.add_argument(
        "--ambient-settings-file",
        default=".ambient_settings_sim.json",
        help="Simulation ambient-light settings file.",
    )
    ui_parser.set_defaults(func=ui_command)

    ping_parser = subparsers.add_parser("ping", help="Call /animation/ping.")
    add_common_url_argument(ping_parser)
    ping_parser.set_defaults(func=ping_command)

    state_parser = subparsers.add_parser("state", help="Call /animation/state.")
    add_common_url_argument(state_parser)
    state_parser.set_defaults(func=state_command)

    uptime_parser = subparsers.add_parser("uptime", help="Call /system/uptime.")
    add_common_url_argument(uptime_parser)
    uptime_parser.set_defaults(func=uptime_command)

    config_parser = subparsers.add_parser("config", help="Call /animation/config.")
    add_common_url_argument(config_parser)
    config_parser.add_argument("--fade-step", type=int, help="New fade_step value.")
    config_parser.add_argument("--frame-delay", type=float, help="New frame_delay_s value.")
    config_parser.set_defaults(func=config_command)

    install_parser = subparsers.add_parser("install", help="Upload a new core source file.")
    add_common_url_argument(install_parser)
    install_parser.add_argument("source", help="Python file defining AnimationCore or CORE_CLASS.")
    install_parser.set_defaults(func=install_command)

    reset_parser = subparsers.add_parser("reset", help="Call /animation/reset.")
    add_common_url_argument(reset_parser)
    reset_parser.set_defaults(func=reset_command)

    request_parser = subparsers.add_parser(
        "request", help="Call the active core JSON API via /animation/core."
    )
    add_common_url_argument(request_parser)
    request_parser.add_argument("path", help="Core API path, e.g. comet-state")
    request_parser.add_argument(
        "--json", help="Optional JSON request body, e.g. '{\"key\": 1}'."
    )
    request_parser.set_defaults(func=request_command)

    ambient_state_parser = subparsers.add_parser("ambient-state", help="Call /ambient/state.")
    add_common_url_argument(ambient_state_parser)
    ambient_state_parser.set_defaults(func=ambient_state_command)

    ambient_set_parser = subparsers.add_parser("ambient-set", help="Call /ambient/config.")
    add_common_url_argument(ambient_set_parser)
    ambient_set_parser.add_argument("--dark-luma", type=float, help="New dark_luma value.")
    ambient_set_parser.add_argument("--bright-luma", type=float, help="New bright_luma value.")
    ambient_set_parser.add_argument(
        "--min-brightness", type=float, help="New min_pixel_brightness value."
    )
    ambient_set_parser.add_argument(
        "--max-brightness", type=float, help="New max_pixel_brightness value."
    )
    ambient_set_parser.add_argument(
        "--sample-interval", type=float, help="New sample_interval_s value."
    )
    ambient_set_parser.add_argument(
        "--smoothing", type=float, help="New brightness_smoothing value."
    )
    ambient_set_parser.add_argument(
        "--manual-agc-gain", type=int, help="New fixed camera agc_gain value."
    )
    ambient_set_parser.add_argument(
        "--manual-aec-value", type=int, help="New fixed camera aec_value value."
    )
    ambient_set_parser.set_defaults(func=ambient_set_command)

    ambient_commit_parser = subparsers.add_parser(
        "ambient-commit", help="Persist current ambient settings."
    )
    add_common_url_argument(ambient_commit_parser)
    ambient_commit_parser.set_defaults(func=ambient_commit_command)

    ambient_reset_parser = subparsers.add_parser(
        "ambient-reset", help="Reset current and persisted ambient settings."
    )
    add_common_url_argument(ambient_reset_parser)
    ambient_reset_parser.set_defaults(func=ambient_reset_command)

    ambient_sample_parser = subparsers.add_parser(
        "ambient-sample",
        help="Capture or provide a sample point and optionally calibrate dark/bright thresholds.",
    )
    add_common_url_argument(ambient_sample_parser)
    ambient_sample_parser.add_argument(
        "role",
        nargs="?",
        choices=("dark", "bright"),
        help="Optional calibration role.",
    )
    ambient_sample_parser.add_argument(
        "--brightness",
        type=float,
        help="Optional brightness to pair with the sampled dark/bright point.",
    )
    ambient_sample_parser.add_argument(
        "--luma",
        type=float,
        help="Override luma instead of capturing it. Useful with the simulator.",
    )
    ambient_sample_parser.set_defaults(func=ambient_sample_command)

    return parser


def main():
    parser = build_parser()
    argv = sys.argv[1:]
    commands = {
        "run",
        "serve",
        "ui",
        "ping",
        "state",
        "uptime",
        "config",
        "install",
        "reset",
        "request",
        "ambient-state",
        "ambient-set",
        "ambient-commit",
        "ambient-reset",
        "ambient-sample",
    }
    if argv and not argv[0].startswith("-") and argv[0] not in commands:
        argv = ["run", "--source", argv[0]] + argv[1:]
    if not argv:
        argv = ["run"]
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return
    try:
        args.func(args)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc.reason}")


if __name__ == "__main__":
    main()
