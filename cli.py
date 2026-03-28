import argparse
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from animation import ClockAnimationCore, ClockPlatform, load_core_class_from_source

ANSI_RESET = "\x1b[0m"
ANSI_CLEAR = "\x1b[2J\x1b[H"
ANSI_HIDE_CURSOR = "\x1b[?25l"
ANSI_SHOW_CURSOR = "\x1b[?25h"
DEFAULT_URL = "http://127.0.0.1:8000"


class MemoryPixels:
    def __init__(self, count):
        self._pixels = [(0, 0, 0)] * count

    def __len__(self):
        return len(self._pixels)

    def __getitem__(self, index):
        return self._pixels[index]

    def __setitem__(self, index, value):
        self._pixels[index] = tuple(value)

    def show(self):
        pass


class SimulatorHost:
    def __init__(self, platform, default_core_cls):
        self.platform = platform
        self.default_core_cls = default_core_cls
        self.core = default_core_cls(platform)
        self.core_source = None
        self._lock = threading.RLock()

    def get_state(self):
        with self._lock:
            state = self.core.get_state()
            state["source"] = "builtin" if self.core_source is None else "uploaded"
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
            self.core.tick()

    def frame_delay(self):
        with self._lock:
            return self.core.config["frame_delay_s"]

    def render(self):
        with self._lock:
            label = f"{self.core.NAME} v{self.core.VERSION}"
            return render_ring(self.platform.pixels, label=label)

    def ping(self):
        with self._lock:
            return {
                "ip": "127.0.0.1",
                "name": self.core.NAME,
                "version": self.core.VERSION,
            }

    def handle_core_api(self, path, data):
        with self._lock:
            return self.core.handle_api(path, data)


def fg(color, text="●"):
    r, g, b = color
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

    for index, color in enumerate(pixels):
        angle = ((index / len(pixels)) * (2 * math.pi)) - (math.pi / 2)
        y = int(round(center_y + math.sin(angle) * radius_y))
        x = int(round(center_x + math.cos(angle) * radius_x))
        grid[y][x] = fg(color)

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


def serve_command(args):
    pixels = MemoryPixels(60)
    platform = ClockPlatform(pixels, time_source=time.localtime, sleeper=lambda _: None)
    host = SimulatorHost(platform, ClockAnimationCore)
    if args.source is not None:
        with open(args.source, "r", encoding="utf-8") as f:
            host.install_source(f.read())
    if args.fade_step is not None or args.frame_delay is not None:
        host.update_config(
            {
                key: value
                for key, value in (
                    ("fade_step", args.fade_step),
                    ("frame_delay_s", args.frame_delay),
                )
                if value is not None
            }
        )

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


def with_base_url(url, path):
    return urllib.parse.urljoin(url.rstrip("/") + "/", path.lstrip("/"))


def ping_command(args):
    print_json(http_json(with_base_url(args.url, "/animation/ping")))


def state_command(args):
    print_json(http_json(with_base_url(args.url, "/animation/state")))


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
    serve_parser.add_argument("--no-render", action="store_true", help="Disable terminal rendering.")
    serve_parser.set_defaults(func=serve_command)

    ping_parser = subparsers.add_parser("ping", help="Call /animation/ping.")
    add_common_url_argument(ping_parser)
    ping_parser.set_defaults(func=ping_command)

    state_parser = subparsers.add_parser("state", help="Call /animation/state.")
    add_common_url_argument(state_parser)
    state_parser.set_defaults(func=state_command)

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

    return parser


def main():
    parser = build_parser()
    argv = sys.argv[1:]
    if argv and argv[0] not in {
        "run",
        "serve",
        "ping",
        "state",
        "config",
        "install",
        "reset",
        "request",
    }:
        argv = ["run"] + argv
    if not argv:
        argv = ["run"]
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Request failed: {exc.reason}")


if __name__ == "__main__":
    main()
