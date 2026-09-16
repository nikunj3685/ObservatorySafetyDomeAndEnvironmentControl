#!/usr/bin/env python3
"""
Quick test: read the DHT11 (inside-box temp/humidity) and the roof reed
switch (closed/not-closed), once every 2 seconds.

Both are safe passive reads - nothing here triggers the relay or the
heater MOSFET. Run with:
    python3 test_dht_reed.py
Stop with Ctrl+C.
"""
import time

import board
import adafruit_dht
import RPi.GPIO as GPIO

# ---- DHT11 (inside box) ----
dht = adafruit_dht.DHT11(board.D4)  # GPIO4 / header pin 7

# ---- Roof reed switch ----
REED_PIN = 16  # GPIO16 / header pin 36
GPIO.setmode(GPIO.BCM)
GPIO.setup(REED_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # LOW = closed (switch shorts to GND)

print("Reading DHT11 + roof reed switch - Ctrl+C to stop")

try:
    while True:
        # DHT11 reads occasionally fail/checksum-error - that's normal for
        # this sensor family, just skip and retry next loop rather than crash.
        try:
            temp_c = dht.temperature
            humidity = dht.humidity
            dht_str = f"Inside box: {temp_c:4.1f} C, {humidity:4.1f} % RH"
        except RuntimeError as e:
            dht_str = f"Inside box: read error ({e.args[0]}) - retrying"

        reed_state = GPIO.input(REED_PIN)
        roof_str = "CLOSED" if reed_state == GPIO.LOW else "NOT CLOSED"

        print(f"{dht_str}   |   Roof: {roof_str}")
        time.sleep(2)

except KeyboardInterrupt:
    print("\nStopped.")
finally:
    GPIO.cleanup()
