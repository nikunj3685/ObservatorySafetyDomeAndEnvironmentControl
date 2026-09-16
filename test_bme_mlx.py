#!/usr/bin/env python3
"""
Quick test: read the BME280 (outside-box temperature/humidity/pressure) and
the MLX90614 (sky/ambient IR temperature) together, once every 2 seconds.
Both live on the same I2C bus (bus 1, GPIO2/3) - BME280 at 0x76, MLX90614
at 0x5a - so one shared I2C object talks to both.

This is deliberately NOT the final Alpaca service - same as the other test
scripts, this just proves the wiring + libraries work before everything
gets folded into the unified Phase 5 service.

Run with:
    python3 test_bme_mlx.py
Stop with Ctrl+C.
"""
import time

import board
import busio
import adafruit_bme280.basic as adafruit_bme280
import adafruit_mlx90614

# ---- Setup ----
i2c = busio.I2C(board.SCL, board.SDA)  # bus 1 - shared by BME280 and MLX90614

bme = adafruit_bme280.Adafruit_BME280_I2C(i2c, address=0x76)  # change to 0x77 if SDO is pulled high
mlx = adafruit_mlx90614.MLX90614(i2c)  # addr 0x5A, default

# Set this to your local sea-level pressure (hPa) for a more accurate altitude
# reading - not critical for the safety-monitor logic, just informational.
bme.sea_level_pressure = 1013.25

print("Reading BME280 (outside box) + MLX90614 (sky/ambient) - Ctrl+C to stop")

try:
    while True:
        outside_c = bme.temperature
        humidity = bme.humidity
        pressure = bme.pressure

        ambient_c = mlx.ambient_temperature
        sky_c = mlx.object_temperature

        print(
            f"Outside: {outside_c:5.1f} C, {humidity:4.1f} % RH, {pressure:6.1f} hPa   |   "
            f"Ambient: {ambient_c:5.1f} C   Sky: {sky_c:5.1f} C   Delta: {ambient_c - sky_c:5.1f} C"
        )

        time.sleep(2)

except KeyboardInterrupt:
    print("\nStopped.")
