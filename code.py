import board
import neopixel
import time
import wifi
import socketpool
import adafruit_ntp
import rtc

PIXEL_PIN = board.IO16
PIXEL_COUNT = 60
BRIGHTNESS = 0.2
FADE_STEP = 51  # Per-frame decrement so a fully lit channel fades out in ~5 frames

# WiFi credentials
try:
    from secrets import secrets
except ImportError:
    print("WiFi secrets are kept in secrets.py, please add them there!")
    raise

pixels = neopixel.NeoPixel(PIXEL_PIN, PIXEL_COUNT, brightness=BRIGHTNESS, auto_write=False)


def wheel(pos):
    """Generate rainbow colors across 0-255 positions."""
    if pos < 85:
        return (pos * 3, 255 - pos * 3, 0)
    elif pos < 170:
        pos -= 85
        return (255 - pos * 3, 0, pos * 3)
    else:
        pos -= 170
        return (0, pos * 3, 255 - pos * 3)


def rainbow_animation_frame(offset):
    """Display one frame of rainbow animation."""
    for i in range(PIXEL_COUNT):
        pixel_index = (i * 256 // PIXEL_COUNT) + offset
        pixels[i] = wheel(pixel_index & 255)
    pixels.show()


def connect_wifi():
    """Blocking connect to WiFi."""
    print("Connecting to WiFi...")

    # Try to connect
    wifi.radio.connect(secrets['ssid'], secrets['password'])

    print(f"Connected to WiFi! IP: {wifi.radio.ipv4_address}")


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

    # Convert Zeller's output (0=Sat) to our format (0=Sun)
    return (h + 6) % 7


def find_nth_weekday(year, month, weekday, n):
    """Find the nth occurrence of a weekday in a month.
    weekday: 0=Sunday, 1=Monday, ..., 6=Saturday
    n: 1=first, 2=second, etc.
    Returns the day of month.
    """
    # Find the first occurrence of the weekday
    first_day_of_month = day_of_week(year, month, 1)
    first_occurrence = 1 + (weekday - first_day_of_month) % 7

    # Calculate the nth occurrence
    return first_occurrence + (n - 1) * 7


def is_dst(year, month, day, hour):
    """Check if date/time is in DST (US rules).
    DST starts: 2nd Sunday in March at 2:00 AM
    DST ends: 1st Sunday in November at 2:00 AM
    """
    # Before March or after November
    if month < 3 or month > 11:
        return False

    # April through October - always DST
    if month > 3 and month < 11:
        return True

    # March - check if we're past 2nd Sunday at 2 AM
    if month == 3:
        dst_start_day = find_nth_weekday(year, 3, 0, 2)  # 2nd Sunday
        if day < dst_start_day:
            return False
        elif day > dst_start_day:
            return True
        else:  # day == dst_start_day
            return hour >= 2

    # November - check if we're before 1st Sunday at 2 AM
    if month == 11:
        dst_end_day = find_nth_weekday(year, 11, 0, 1)  # 1st Sunday
        if day < dst_end_day:
            return True
        elif day > dst_end_day:
            return False
        else:  # day == dst_end_day
            return hour < 2

    return False


def get_pacific_tz_offset():
    """Get timezone offset for Pacific Time with DST handling.
    Returns -7 during PDT (Daylight Time) or -8 during PST (Standard Time).
    """
    # Get current UTC time to determine DST
    # We'll get it after first sync and check
    current = time.localtime()

    if is_dst(current.tm_year, current.tm_mon, current.tm_mday, current.tm_hour):
        return -7  # PDT (Pacific Daylight Time)
    else:
        return -8  # PST (Pacific Standard Time)


def sync_time():
    """Sync time using NTP with DST-aware Pacific Time."""
    print("Syncing time via NTP...")
    pool = socketpool.SocketPool(wifi.radio)

    # First sync with UTC to determine DST
    ntp = adafruit_ntp.NTP(pool, tz_offset=0)
    rtc.RTC().datetime = ntp.datetime

    # Now get the correct offset and resync
    tz_offset = get_pacific_tz_offset()
    ntp = adafruit_ntp.NTP(pool, tz_offset=tz_offset)
    rtc.RTC().datetime = ntp.datetime

    tz_name = "PDT" if tz_offset == -7 else "PST"
    print(f"Time synced ({tz_name}): {time.localtime()}")


def get_led_position_for_hour(hour):
    """Convert hour (0-23) to LED position (0-59).
    Maps 12-hour format to 60 LEDs, centered at 0, 5, 10, 15, etc.
    At 12:00, center is at LED 0 (displays 58, 59, 0, 1, 2).
    """
    hour_12 = hour % 12
    return hour_12 * 5


def saturating_decrement(color, amount):
    """Fade a pixel by subtracting from each channel without going below zero."""
    return tuple(max(0, channel - amount) for channel in color)


def display_clock(current_time):
    """Display current time on the LED ring."""
    hour = current_time.tm_hour
    minute = current_time.tm_min
    second = current_time.tm_sec

    # Fade the existing display in place.
    for i in range(PIXEL_COUNT):
        pixels[i] = saturating_decrement(pixels[i], FADE_STEP)

    # Get LED positions
    hour_pos = get_led_position_for_hour(hour) - 1
    minute_pos = minute - 1
    second_pos = second - 1

    # Hour (3 red pixels centered around hour position).
    for offset in range(-1, 2):
        pos = (hour_pos + offset) % PIXEL_COUNT
        _, g, b = pixels[pos]
        pixels[pos] = (255, g, b)

    # Minute (blue pixel).
    r, g, _ = pixels[minute_pos]
    pixels[minute_pos] = (r, g, 255)

    # Second (green pixel).
    r, _, b = pixels[second_pos]
    pixels[second_pos] = (r, 255, b)

    # Update LED strip
    pixels.show()

    # Update at ~30 FPS for smooth fading
    time.sleep(0.033)


def main():
    """Main program loop."""
    rainbow_animation_frame(0)
    connect_wifi()

    print("Starting clock display...")

    last_sync_hour = -1
    while True:
        current_time = time.localtime()
        if current_time.tm_hour != last_sync_hour:
            try:
                sync_time()
            except Exception as e:
                print(f"Background NTP sync failed: {e}")
            last_sync_hour = current_time.tm_hour
            # In case the update was slow
            current_time = time.localtime()

        # Display the clock
        display_clock(current_time)

main()
