import board
import neopixel

PIXEL_PIN = board.IO16
PIXEL_COUNT = 60
BRIGHTNESS = 0.2

pixels = neopixel.NeoPixel(PIXEL_PIN, PIXEL_COUNT, brightness=BRIGHTNESS, auto_write=False)

def main():
    pixels.fill((128, 128, 128))
    pixels[59] = (128, 0, 0)
    pixels[14] = (0, 128, 0)
    pixels[29] = (0, 0, 128)
    pixels[44] = (128, 0, 128)
    pixels.show()

main()
