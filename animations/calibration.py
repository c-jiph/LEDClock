try:
    from animation import ClockAnimationCore
except ImportError:
    pass


class AnimationCore(ClockAnimationCore):
    NAME = "calibration"
    VERSION = 1

    def tick(self, current_time=None):
        for i in range(self.platform.pixel_count):
            self.platform.set_pixel(i, (0, 0, 0))

        markers = {
            0: (255, 255, 255),  # 12 o'clock
            15: (255, 0, 0),    # 3 o'clock
            30: (0, 255, 0),    # 6 o'clock
            45: (0, 0, 255),    # 9 o'clock
        }
        for position, color in markers.items():
            self.platform.set_pixel(position, color)

        self.platform.show()
        self.platform.sleep(self.config["frame_delay_s"])

    def handle_api(self, path, data):
        if path == "state":
            return {
                "name": self.NAME,
                "positions": {
                    "12": 0,
                    "3": 15,
                    "6": 30,
                    "9": 45,
                },
            }
        return {"error": f"Unknown calibration API path: {path}", "data": data}
