#!/usr/bin/env python3
"""
Quick end-to-end test: read the MLX90614 (sky/ambient IR temperature) and
display the values on the SSD1306 OLED, once a second.

This is deliberately NOT the final Alpaca service - it's just proof that
the I2C wiring + Python libraries work together, the same way i2cdetect
proved the raw bus wiring worked. Once BME280/DHT11/rain/reed/relay/heater
are all wired in too, this gets folded into the real unified service
(Phase 5) instead of staying a standalone script.

UPDATED (2026-09): the OLED now lives on its own separate I2C bus
(i2c-2, enabled via the "dtoverlay=i2c2-pi5,pins_12_13" line in
/boot/firmware/config.txt, GPIO12=SDA / GPIO13=SCL) instead of sharing
the MLX90614/BME280 bus (bus 1, GPIO2/3). This means a noisy/marginal
long cable run out to the display can't stall or glitch the sensor bus
that the safety-monitor logic actually depends on.

Run with:
    python3 test_mlx_oled.py
Stop with Ctrl+C.
"""
import time

import board
import busio
from adafruit_extended_bus import ExtendedI2C
import adafruit_mlx90614
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

# ---- Setup ----
i2c_sensors = busio.I2C(board.SCL, board.SDA)  # bus 1 (GPIO2/3) - MLX90614, BME280 later
i2c_display = ExtendedI2C(2)                   # bus 2 (GPIO12/13) - OLED only, isolated

mlx = adafruit_mlx90614.MLX90614(i2c_sensors)  # addr 0x5A, default

# Most of these small OLEDs are 128x64 - change here if yours is 128x32.
OLED_WIDTH = 128
OLED_HEIGHT = 64
oled = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c_display)  # addr 0x3C, default

font = ImageFont.load_default()

print("Reading MLX90614 + updating OLED - Ctrl+C to stop")

try:
    while True:
        ambient_c = mlx.ambient_temperature
        sky_c = mlx.object_temperature  # "object" = whatever the sensor is pointed at (the sky)

        print(f"Ambient: {ambient_c:5.1f} C   Sky: {sky_c:5.1f} C   Delta: {ambient_c - sky_c:5.1f} C")

        # Draw a simple status frame on the OLED
        image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, OLED_WIDTH, OLED_HEIGHT), outline=0, fill=0)
        draw.text((0, 0), "Weather Station (test)", font=font, fill=255)
        draw.text((0, 16), f"Ambient: {ambient_c:5.1f} C", font=font, fill=255)
        draw.text((0, 28), f"Sky:     {sky_c:5.1f} C", font=font, fill=255)
        draw.text((0, 40), f"Delta:   {ambient_c - sky_c:5.1f} C", font=font, fill=255)
        oled.image(image)
        oled.show()

        time.sleep(1)

except KeyboardInterrupt:
    print("\nStopped.")
    oled.fill(0)
    oled.show()
