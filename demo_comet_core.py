try:
    from animation import ClockAnimationCore, saturating_decrement
except ImportError:
    pass


class AnimationCore(ClockAnimationCore):
    NAME = "comet"
    VERSION = 1

    def __init__(self, platform, config=None):
        super().__init__(platform, config=config)
        self.phase = 0

    def tick(self, current_time=None):
        for i in range(self.platform.pixel_count):
            color = self.platform.get_pixel(i)
            self.platform.set_pixel(i, saturating_decrement(color, self.config["fade_step"]))

        comet_head = self.phase % self.platform.pixel_count
        for tail, brightness in enumerate((255, 180, 96, 48)):
            pos = (comet_head - tail) % self.platform.pixel_count
            self.platform.set_pixel(pos, (brightness, brightness // 3, 255))

        marker = (comet_head + 20) % self.platform.pixel_count
        self.platform.set_pixel(marker, (0, 255, 80))

        self.platform.show()
        self.platform.sleep(self.config["frame_delay_s"])
        self.phase = (self.phase + 1) % self.platform.pixel_count

    def handle_api(self, path, data):
        if path == "state":
            return {
                "name": self.NAME,
                "phase": self.phase,
                "marker": (self.phase + 20) % self.platform.pixel_count,
            }
        if path == "reset":
            self.phase = 0
            return {"ok": True, "phase": self.phase}
        return {"error": f"Unknown comet API path: {path}", "data": data}
