import builtins
import time

DEFAULT_CONFIG = {
    "fade_step": 51,
    "frame_delay_s": 0.033,
}


def get_led_position_for_hour(hour):
    """Convert hour (0-23) to LED position (0-59)."""
    hour_12 = hour % 12
    return hour_12 * 5


def saturating_decrement(color, amount):
    """Fade a pixel by subtracting from each channel without going below zero."""
    return tuple(max(0, channel - amount) for channel in color)


def wheel(pos):
    """Generate rainbow colors across 0-255 positions."""
    if pos < 85:
        return (pos * 3, 255 - pos * 3, 0)
    if pos < 170:
        pos -= 85
        return (255 - pos * 3, 0, pos * 3)
    pos -= 170
    return (0, pos * 3, 255 - pos * 3)


class ClockPlatform:
    def __init__(self, pixels, time_source=time.localtime, sleeper=time.sleep, logger=print):
        self.pixels = pixels
        self.time_source = time_source
        self.sleeper = sleeper
        self.logger = logger

    @property
    def pixel_count(self):
        return len(self.pixels)

    def get_pixel(self, index):
        return self.pixels[index]

    def set_pixel(self, index, color):
        self.pixels[index] = color

    def show(self):
        self.pixels.show()

    def now(self):
        return self.time_source()

    def sleep(self, seconds):
        self.sleeper(seconds)


class ClockAnimationCore:
    NAME = "clock"
    VERSION = 1

    def __init__(self, platform, config=None):
        self.platform = platform
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.update_config(config)

    def tick(self, current_time=None):
        current_time = current_time or self.platform.now()
        hour = current_time.tm_hour
        minute = current_time.tm_min
        second = current_time.tm_sec

        for i in range(self.platform.pixel_count):
            color = self.platform.get_pixel(i)
            self.platform.set_pixel(i, saturating_decrement(color, self.config["fade_step"]))

        hour_pos = get_led_position_for_hour(hour) - 1
        minute_pos = minute - 1
        second_pos = second - 1

        for offset in range(-1, 2):
            pos = (hour_pos + offset) % self.platform.pixel_count
            _, g, b = self.platform.get_pixel(pos)
            self.platform.set_pixel(pos, (255, g, b))

        r, g, _ = self.platform.get_pixel(minute_pos)
        self.platform.set_pixel(minute_pos, (r, g, 255))

        r, _, b = self.platform.get_pixel(second_pos)
        self.platform.set_pixel(second_pos, (r, 255, b))

        self.platform.show()
        self.platform.sleep(self.config["frame_delay_s"])

    def handle_api(self, path, data):
        return {"error": f"Unknown core API path: {path}"}

    def get_state(self):
        return {
            "name": self.NAME,
            "version": self.VERSION,
            "config": dict(self.config),
        }

    def update_config(self, data):
        if "fade_step" in data:
            fade_step = int(data["fade_step"])
            if fade_step < 0 or fade_step > 255:
                raise ValueError("fade_step must be between 0 and 255")
            self.config["fade_step"] = fade_step

        if "frame_delay_s" in data:
            frame_delay_s = float(data["frame_delay_s"])
            if frame_delay_s < 0:
                raise ValueError("frame_delay_s must be >= 0")
            self.config["frame_delay_s"] = frame_delay_s

        return self.get_state()


def load_core_class_from_source(source):
    scope = {}
    globals_dict = {
        "__builtins__": builtins,
        "ClockAnimationCore": ClockAnimationCore,
        "get_led_position_for_hour": get_led_position_for_hour,
        "saturating_decrement": saturating_decrement,
        "wheel": wheel,
    }
    exec(source, globals_dict, scope)
    core_cls = scope.get("AnimationCore") or scope.get("CORE_CLASS")
    if core_cls is None:
        raise ValueError("Uploaded code must define AnimationCore or CORE_CLASS")
    if not issubclass(core_cls, ClockAnimationCore):
        raise ValueError("Uploaded core must subclass ClockAnimationCore")
    for attr in ("NAME", "VERSION", "tick", "get_state", "update_config"):
        if not hasattr(core_cls, attr):
            raise ValueError(f"Missing required attribute: {attr}")
    return core_cls
