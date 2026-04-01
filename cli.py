import argparse
import json
import math
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


def web_ui_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Clock Control Desk</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --pico-font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      --pico-font-size: 15px;
      --pico-form-element-spacing-vertical: 0.6rem;
      --pico-form-element-spacing-horizontal: 0.7rem;
      --pico-block-spacing-vertical: 0.85rem;
      --pico-block-spacing-horizontal: 0.95rem;
      --pico-typography-spacing-vertical: 0.75rem;
      --panel: rgba(255,255,255,0.78);
      --panel-strong: rgba(255,255,255,0.9);
      --accent: #ef8354;
      --accent-2: #2d728f;
      --ink: #15202b;
      --muted: #5c6770;
      --line: rgba(21,32,43,0.12);
    }
    body {
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(239,131,84,0.22), transparent 28rem),
        radial-gradient(circle at top right, rgba(45,114,143,0.18), transparent 24rem),
        linear-gradient(180deg, #f4efe8 0%, #eef4f6 100%);
      color: var(--ink);
    }
    main.container {
      max-width: 1380px;
      padding-top: 1.1rem;
      padding-bottom: 2rem;
    }
    header.hero {
      display: grid;
      gap: 0.85rem;
      grid-template-columns: minmax(0, 1.55fr) minmax(330px, 0.95fr);
      align-items: end;
      margin-bottom: 0.85rem;
    }
    .hero-card, article {
      backdrop-filter: blur(12px);
      background: var(--panel);
      border: 1px solid var(--line);
      box-shadow: 0 20px 50px rgba(26, 33, 44, 0.08);
    }
    .hero-card {
      border-radius: 1.2rem;
      padding: 1.1rem 1.3rem;
    }
    .hero-card h1 {
      margin-bottom: 0.2rem;
      font-size: clamp(1.8rem, 3.5vw, 3rem);
      letter-spacing: -0.04em;
      line-height: 0.95;
    }
    .hero-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 0.65rem;
    }
    .metric {
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 1rem;
      padding: 0.8rem 0.85rem;
    }
    .metric strong {
      display: block;
      font-size: 1.15rem;
      line-height: 1.05;
    }
    .muted {
      color: var(--muted);
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(300px, 390px) minmax(360px, 1fr) minmax(300px, 390px);
      gap: 0.85rem;
      align-items: start;
    }
    article {
      border-radius: 1.2rem;
      padding: 0.95rem;
      margin: 0;
    }
    article h2 {
      font-size: 0.92rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 0.7rem;
    }
    .stack { display: grid; gap: 0.85rem; }
    .pill-row {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      align-items: center;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      border-radius: 999px;
      padding: 0.28rem 0.6rem;
      background: rgba(255,255,255,0.66);
      border: 1px solid var(--line);
      font-size: 0.82rem;
      line-height: 1.1;
    }
    .grid-2, .grid-3 {
      display: grid;
      gap: 0.65rem;
    }
    .grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .field label { font-size: 0.76rem; color: var(--muted); margin-bottom: 0.2rem; }
    .field input, .field textarea, .field select { margin-bottom: 0; }
    .field .pair {
      display: grid;
      grid-template-columns: 1fr 7rem;
      gap: 0.45rem;
      align-items: center;
    }
    .field .pair input[type="range"] { margin-bottom: 0; }
    textarea {
      min-height: 6.5rem;
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      resize: vertical;
    }
    pre {
      margin: 0;
      max-height: 15rem;
      overflow: auto;
      background: rgba(14, 19, 24, 0.92);
      color: #d5e7ff;
      border-radius: 0.9rem;
      padding: 0.75rem;
      font-size: 0.78rem;
    }
    .preview-shell {
      display: grid;
      gap: 0.6rem;
      place-items: center;
    }
    #ring {
      width: min(100%, 22rem);
      aspect-ratio: 1;
      border-radius: 50%;
      background:
        radial-gradient(circle, rgba(255,255,255,0.88) 0%, rgba(255,255,255,0.55) 52%, rgba(255,255,255,0.28) 100%);
      border: 1px solid var(--line);
      box-shadow: inset 0 10px 30px rgba(255,255,255,0.7);
    }
    .chart-shell {
      min-height: 18rem;
      display: grid;
    }
    .log {
      background: rgba(14, 19, 24, 0.92);
      color: #edf6f9;
      border-radius: 0.9rem;
      padding: 0.75rem;
      max-height: 12rem;
      overflow: auto;
      font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
      font-size: 0.78rem;
    }
    .log-entry { white-space: pre-wrap; margin-bottom: 0.65rem; }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 0.6rem;
    }
    .compact button, .compact input, .compact textarea { margin-bottom: 0; }
    .ok { color: #0f766e; }
    .warn { color: #b45309; }
    .bad { color: #b91c1c; }
    @media (max-width: 1320px) {
      .layout {
        grid-template-columns: minmax(280px, 360px) minmax(320px, 1fr);
      }
      .layout > .stack:last-child {
        grid-column: 1 / -1;
      }
    }
    @media (max-width: 1080px) {
      header.hero, .layout { grid-template-columns: 1fr; }
      .layout > .stack:last-child {
        grid-column: auto;
      }
      .hero-metrics {
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }
    }
    @media (max-width: 720px) {
      :root {
        --pico-font-size: 14px;
      }
      .hero-metrics, .grid-2, .grid-3 {
        grid-template-columns: 1fr;
      }
      #ring {
        width: min(100%, 18rem);
      }
    }
  </style>
</head>
<body>
  <main class="container">
    <header class="hero">
      <section class="hero-card">
        <div class="pill-row">
          <span class="pill"><strong id="modeBadge">Loading</strong></span>
          <span class="pill">Target <span id="targetUrl" class="muted">pending</span></span>
          <span class="pill">Core <span id="coreSummary" class="muted">pending</span></span>
        </div>
        <h1>Clock Control Desk</h1>
        <p class="muted">Browser UI for live simulation, device proxying, ambient calibration, and hot-swappable animation cores.</p>
      </section>
      <section class="hero-metrics">
        <div class="metric">
          <span class="muted">Ambient</span>
          <strong id="metricLuma">--</strong>
          <span class="muted">last measured luma</span>
        </div>
        <div class="metric">
          <span class="muted">Target</span>
          <strong id="metricTarget">--</strong>
          <span class="muted">target LED brightness</span>
        </div>
        <div class="metric">
          <span class="muted">Applied</span>
          <strong id="metricApplied">--</strong>
          <span class="muted">current pixel brightness</span>
        </div>
      </section>
    </header>

    <section class="layout">
      <div class="stack">
        <article>
          <h2>Connection</h2>
          <div class="actions compact">
            <button id="refreshButton" class="contrast">Refresh</button>
          </div>
          <div class="pill-row" style="margin-top:0.8rem">
            <span class="pill">Device IP <span id="ipSummary" class="muted">--</span></span>
            <span class="pill">Version <span id="versionSummary" class="muted">--</span></span>
            <span class="pill">Source <span id="sourceSummary" class="muted">--</span></span>
            <span class="pill">Uptime <span id="uptimeSummary" class="muted">--</span></span>
          </div>
          <pre id="statePre">{}</pre>
        </article>

        <article>
          <h2>Animation</h2>
          <div class="grid-2">
            <div class="field">
              <label for="fadeStep">Fade Step</label>
              <input id="fadeStep" type="number" min="1" step="1">
            </div>
            <div class="field">
              <label for="frameDelay">Frame Delay (s)</label>
              <input id="frameDelay" type="number" min="0.001" step="0.001">
            </div>
          </div>
          <div class="actions compact" style="margin-top:0.8rem">
            <button id="applyAnimation">Apply Config</button>
            <button id="resetCore" class="secondary">Reset Core</button>
          </div>
          <div class="field" style="margin-top:1rem">
            <label for="installFile">Upload Core Source</label>
            <input id="installFile" type="file" accept=".py,text/x-python">
          </div>
          <div class="field">
            <label for="installSource">Or Paste Core Source</label>
            <textarea id="installSource" placeholder="class AnimationCore(ClockAnimationCore): ..."></textarea>
          </div>
          <div class="actions compact">
            <button id="installCore">Install Core</button>
          </div>
        </article>

        <article>
          <h2>Core API</h2>
          <div class="field">
            <label for="corePath">Path</label>
            <input id="corePath" type="text" placeholder="state">
          </div>
          <div class="field">
            <label for="corePayload">JSON Payload</label>
            <textarea id="corePayload">{}</textarea>
          </div>
          <div class="actions compact">
            <button id="sendCoreRequest">Send Request</button>
          </div>
          <pre id="coreResponsePre">{}</pre>
        </article>
      </div>

      <div class="stack">
        <article>
          <h2>Simulation Preview</h2>
          <div class="preview-shell">
            <svg id="ring" viewBox="0 0 100 100" aria-label="LED ring preview"></svg>
            <div class="pill-row">
              <span class="pill">Preview <span id="previewMode" class="muted">pending</span></span>
              <span class="pill">Pixels <span id="pixelCount" class="muted">0</span></span>
              <span class="pill">Brightness <span id="pixelBrightness" class="muted">--</span></span>
            </div>
          </div>
        </article>

        <article>
          <h2>Light Monitor</h2>
          <div class="chart-shell">
            <canvas id="ambientChart"></canvas>
          </div>
          <p class="muted" style="margin:0.75rem 0 0">Auto-refreshes every 5 seconds. In simulation mode the pixel preview updates more frequently.</p>
        </article>
      </div>

      <div class="stack">
        <article>
          <h2>Ambient Tuning</h2>
          <div class="grid-2">
            <div class="field">
              <label for="darkLuma">Dark Luma</label>
              <div class="pair">
                <input id="darkLuma" type="range" min="0" max="1" step="0.001">
                <input id="darkLumaNumber" type="number" min="0" max="1" step="0.001">
              </div>
            </div>
            <div class="field">
              <label for="brightLuma">Bright Luma</label>
              <div class="pair">
                <input id="brightLuma" type="range" min="0" max="1" step="0.001">
                <input id="brightLumaNumber" type="number" min="0" max="1" step="0.001">
              </div>
            </div>
            <div class="field">
              <label for="minBrightness">Min Pixel Brightness</label>
              <div class="pair">
                <input id="minBrightness" type="range" min="0" max="1" step="0.001">
                <input id="minBrightnessNumber" type="number" min="0" max="1" step="0.001">
              </div>
            </div>
            <div class="field">
              <label for="maxBrightness">Max Pixel Brightness</label>
              <div class="pair">
                <input id="maxBrightness" type="range" min="0" max="1" step="0.001">
                <input id="maxBrightnessNumber" type="number" min="0" max="1" step="0.001">
              </div>
            </div>
            <div class="field">
              <label for="sampleInterval">Sample Interval (s)</label>
              <input id="sampleInterval" type="number" min="0.1" step="0.1">
            </div>
            <div class="field">
              <label for="smoothing">Brightness Smoothing</label>
              <div class="pair">
                <input id="smoothing" type="range" min="0" max="1" step="0.01">
                <input id="smoothingNumber" type="number" min="0" max="1" step="0.01">
              </div>
            </div>
            <div class="field">
              <label for="manualAgcGain">Manual AGC Gain</label>
              <input id="manualAgcGain" type="number" min="0" step="1">
            </div>
            <div class="field">
              <label for="manualAecValue">Manual AEC Value</label>
              <input id="manualAecValue" type="number" min="0" step="1">
            </div>
          </div>
          <div class="actions compact" style="margin-top:0.9rem">
            <button id="applyAmbient">Apply Ambient Settings</button>
            <button id="commitAmbient">Commit</button>
            <button id="resetAmbient" class="secondary">Reset Defaults</button>
          </div>
          <div class="grid-3" style="margin-top:0.9rem">
            <div class="field">
              <label for="sampleBrightness">Sample Brightness Override</label>
              <input id="sampleBrightness" type="number" min="0" max="1" step="0.001">
            </div>
            <div class="field">
              <label for="sampleLuma">Sample Luma Override</label>
              <input id="sampleLuma" type="number" min="0" max="1" step="0.001" placeholder="sim only">
            </div>
            <div class="field">
              <label>&nbsp;</label>
              <div class="actions compact">
                <button id="sampleDark" class="outline">Sample Dark</button>
                <button id="sampleBright" class="outline">Sample Bright</button>
              </div>
            </div>
          </div>
        </article>

        <article>
          <h2>Activity</h2>
          <div id="log" class="log"></div>
        </article>
      </div>
    </section>
  </main>
  <script>
    const state = {
      dashboard: null,
      chart: null,
      statusTimer: null,
      fastTimer: null,
      formPrimed: false,
    };

    function $(id) { return document.getElementById(id); }
    function fmt(value, digits = 3) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
      return Number(value).toFixed(digits);
    }
    function fmtDuration(seconds) {
      if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "--";
      let remaining = Math.max(0, Math.floor(Number(seconds)));
      const hours = Math.floor(remaining / 3600);
      remaining -= hours * 3600;
      const minutes = Math.floor(remaining / 60);
      const secs = remaining - (minutes * 60);
      if (hours > 0) return `${hours}h ${minutes}m ${secs}s`;
      if (minutes > 0) return `${minutes}m ${secs}s`;
      return `${secs}s`;
    }
    function log(message, level = "ok") {
      const row = document.createElement("div");
      row.className = "log-entry " + level;
      row.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
      $("log").prepend(row);
    }
    async function api(path, method = "GET", payload = null) {
      const options = { method, headers: {} };
      if (payload !== null) {
        options.headers["Content-Type"] = "application/json";
        options.body = JSON.stringify(payload);
      }
      const response = await fetch(path, options);
      const text = await response.text();
      let data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (error) {
          throw new Error(`Invalid JSON from ${path}: ${text}`);
        }
      }
      if (!response.ok) {
        throw new Error(data && data.error ? data.error : `HTTP ${response.status}`);
      }
      return data;
    }
    function bindPair(rangeId, numberId) {
      const range = $(rangeId);
      const number = $(numberId);
      const sync = (source, target) => {
        target.value = source.value;
      };
      range.addEventListener("input", () => sync(range, number));
      number.addEventListener("input", () => sync(number, range));
    }
    function buildChart() {
      const ctx = $("ambientChart");
      state.chart = new Chart(ctx, {
        type: "line",
        data: {
          labels: [],
          datasets: [
            { label: "Ambient Luma", data: [], borderColor: "#2d728f", backgroundColor: "rgba(45,114,143,0.18)", tension: 0.25, yAxisID: "luma" },
            { label: "Target Brightness", data: [], borderColor: "#ef8354", backgroundColor: "rgba(239,131,84,0.16)", tension: 0.25, yAxisID: "brightness" },
            { label: "Applied Brightness", data: [], borderColor: "#2a9d8f", backgroundColor: "rgba(42,157,143,0.16)", tension: 0.25, yAxisID: "brightness" }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          scales: {
            luma: { type: "linear", position: "left", min: 0, max: 1 },
            brightness: { type: "linear", position: "right", min: 0, max: 1, grid: { drawOnChartArea: false } }
          },
          plugins: {
            legend: { position: "bottom" }
          }
        }
      });
    }
    function renderChart(history) {
      const entries = history || [];
      state.chart.data.labels = entries.map((entry) => new Date(entry.ts * 1000).toLocaleTimeString());
      state.chart.data.datasets[0].data = entries.map((entry) => entry.last_luma);
      state.chart.data.datasets[1].data = entries.map((entry) => entry.last_target_brightness);
      state.chart.data.datasets[2].data = entries.map((entry) => entry.last_applied_brightness);
      state.chart.update();
    }
    function renderRing(pixelState) {
      const ring = $("ring");
      ring.innerHTML = "";
      if (!pixelState || !pixelState.pixels) {
        $("previewMode").textContent = "remote-only";
        ring.innerHTML = '<text x="50" y="52" text-anchor="middle" fill="#5c6770" font-size="3.8">Pixel preview available in simulation mode</text>';
        $("pixelCount").textContent = "0";
        $("pixelBrightness").textContent = "--";
        return;
      }
      $("previewMode").textContent = "simulation";
      $("pixelCount").textContent = String(pixelState.count);
      $("pixelBrightness").textContent = fmt(pixelState.brightness);
      const cx = 50;
      const cy = 50;
      const radius = 34;
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("x", "50");
      label.setAttribute("y", "53");
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("font-size", "4.6");
      label.setAttribute("fill", "#45515c");
      label.textContent = state.dashboard?.state?.name || "clock";
      ring.appendChild(label);
      pixelState.pixels.forEach((pixel, index) => {
        const angle = (index / pixelState.count) * (Math.PI * 2) - Math.PI / 2;
        const x = cx + Math.cos(angle) * radius;
        const y = cy + Math.sin(angle) * radius;
        const led = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        led.setAttribute("cx", x.toFixed(2));
        led.setAttribute("cy", y.toFixed(2));
        led.setAttribute("r", "3.3");
        led.setAttribute("fill", `rgb(${pixel[0]}, ${pixel[1]}, ${pixel[2]})`);
        led.setAttribute("stroke", "rgba(21,32,43,0.18)");
        led.setAttribute("stroke-width", "0.5");
        ring.appendChild(led);
      });
    }
    function primeForms(dashboard) {
      const config = dashboard.state?.config || {};
      $("fadeStep").value = config.fade_step ?? "";
      $("frameDelay").value = config.frame_delay_s ?? "";
      const settings = dashboard.ambient?.settings || {};
      const mapping = [
        ["darkLuma", "darkLumaNumber", settings.dark_luma],
        ["brightLuma", "brightLumaNumber", settings.bright_luma],
        ["minBrightness", "minBrightnessNumber", settings.min_pixel_brightness],
        ["maxBrightness", "maxBrightnessNumber", settings.max_pixel_brightness],
        ["smoothing", "smoothingNumber", settings.brightness_smoothing],
      ];
      mapping.forEach(([rangeId, numberId, value]) => {
        $(rangeId).value = value ?? 0;
        $(numberId).value = value ?? 0;
      });
      $("sampleInterval").value = settings.sample_interval_s ?? "";
      $("manualAgcGain").value = settings.manual_agc_gain ?? "";
      $("manualAecValue").value = settings.manual_aec_value ?? "";
    }
    function renderDashboard(dashboard) {
      state.dashboard = dashboard;
      $("modeBadge").textContent = dashboard.mode;
      $("targetUrl").textContent = dashboard.target_url || "local simulator";
      $("coreSummary").textContent = `${dashboard.state?.name || "--"} v${dashboard.state?.version || "--"}`;
      $("ipSummary").textContent = dashboard.ping?.ip || "--";
      $("versionSummary").textContent = dashboard.state?.version ?? "--";
      $("sourceSummary").textContent = dashboard.state?.source || "--";
      $("uptimeSummary").textContent = fmtDuration(
        dashboard.uptime?.uptime_s ?? dashboard.state?.uptime_s ?? dashboard.ping?.uptime_s
      );
      $("statePre").textContent = JSON.stringify(dashboard.state, null, 2);
      $("metricLuma").textContent = fmt(dashboard.ambient?.last_luma);
      $("metricTarget").textContent = fmt(dashboard.ambient?.last_target_brightness);
      $("metricApplied").textContent = fmt(dashboard.ambient?.last_applied_brightness);
      renderRing(dashboard.pixels);
      renderChart(dashboard.history);
      if (!state.formPrimed) {
        primeForms(dashboard);
        state.formPrimed = true;
      }
    }
    async function refreshDashboard() {
      const dashboard = await api("/api/dashboard");
      renderDashboard(dashboard);
    }
    function ambientPayload() {
      const payload = {
        dark_luma: Number($("darkLumaNumber").value),
        bright_luma: Number($("brightLumaNumber").value),
        min_pixel_brightness: Number($("minBrightnessNumber").value),
        max_pixel_brightness: Number($("maxBrightnessNumber").value),
        sample_interval_s: Number($("sampleInterval").value),
        brightness_smoothing: Number($("smoothingNumber").value),
      };
      const agc = $("manualAgcGain").value.trim();
      const aec = $("manualAecValue").value.trim();
      if (agc !== "") payload.manual_agc_gain = Number(agc);
      if (aec !== "") payload.manual_aec_value = Number(aec);
      return payload;
    }
    function samplePayload(role) {
      const payload = { role };
      const brightness = $("sampleBrightness").value.trim();
      const luma = $("sampleLuma").value.trim();
      if (brightness !== "") payload.brightness = Number(brightness);
      if (luma !== "") payload.luma = Number(luma);
      return payload;
    }
    async function runAction(label, fn) {
      try {
        const result = await fn();
        if (result) {
          log(`${label} ok`);
        } else {
          log(`${label} complete`);
        }
        await refreshDashboard();
        return result;
      } catch (error) {
        log(`${label} failed: ${error.message}`, "bad");
        throw error;
      }
    }
    function startPolling() {
      if (state.statusTimer) clearInterval(state.statusTimer);
      if (state.fastTimer) clearInterval(state.fastTimer);
      state.statusTimer = setInterval(() => {
        refreshDashboard().catch((error) => log(`refresh failed: ${error.message}`, "bad"));
      }, 5000);
      state.fastTimer = setInterval(() => {
        if (state.dashboard?.mode !== "simulation") return;
        refreshDashboard().catch((error) => log(`preview refresh failed: ${error.message}`, "bad"));
      }, 1000);
    }
    async function installFromCurrentInputs() {
      const file = $("installFile").files[0];
      let source = $("installSource").value;
      if (file) {
        source = await file.text();
        $("installSource").value = source;
      }
      if (!source.trim()) {
        throw new Error("core source is empty");
      }
      const response = await api("/api/animation/install", "POST", { source });
      $("coreResponsePre").textContent = JSON.stringify(response, null, 2);
    }
    async function sendCoreRequest() {
      const path = $("corePath").value.trim();
      if (!path) throw new Error("core path is required");
      let payload = {};
      const raw = $("corePayload").value.trim();
      if (raw) payload = JSON.parse(raw);
      const response = await api("/api/animation/core", "POST", { path, data: payload });
      $("coreResponsePre").textContent = JSON.stringify(response, null, 2);
    }
    window.addEventListener("DOMContentLoaded", async () => {
      bindPair("darkLuma", "darkLumaNumber");
      bindPair("brightLuma", "brightLumaNumber");
      bindPair("minBrightness", "minBrightnessNumber");
      bindPair("maxBrightness", "maxBrightnessNumber");
      bindPair("smoothing", "smoothingNumber");
      buildChart();

      $("refreshButton").addEventListener("click", () => runAction("refresh", refreshDashboard));
      $("applyAnimation").addEventListener("click", () => runAction("animation config", async () => {
        await api("/api/animation/config", "PUT", {
          fade_step: Number($("fadeStep").value),
          frame_delay_s: Number($("frameDelay").value),
        });
      }));
      $("resetCore").addEventListener("click", () => runAction("core reset", () => api("/api/animation/reset", "POST", {})));
      $("installCore").addEventListener("click", () => runAction("core install", installFromCurrentInputs));
      $("sendCoreRequest").addEventListener("click", () => runAction("core request", sendCoreRequest));
      $("applyAmbient").addEventListener("click", () => runAction("ambient config", () => api("/api/ambient/config", "PUT", ambientPayload())));
      $("commitAmbient").addEventListener("click", () => runAction("ambient commit", () => api("/api/ambient/commit", "POST", {})));
      $("resetAmbient").addEventListener("click", () => runAction("ambient reset", () => api("/api/ambient/reset", "POST", {})));
      $("sampleDark").addEventListener("click", () => runAction("ambient dark sample", () => api("/api/ambient/sample", "POST", samplePayload("dark"))));
      $("sampleBright").addEventListener("click", () => runAction("ambient bright sample", () => api("/api/ambient/sample", "POST", samplePayload("bright"))));

      try {
        await refreshDashboard();
        startPolling();
        log("UI ready");
      } catch (error) {
        log(`initial load failed: ${error.message}`, "bad");
      }
    });
  </script>
</body>
</html>
"""


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
                if path == "/" and method == "GET":
                    self._send_html(200, web_ui_html())
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

        def _send_html(self, status_code, body):
            encoded = body.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def _send_json(self, status_code, payload):
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

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
