#!/usr/bin/env python3
"""
Quick test: toggle the roof relay on GPIO23 on and off every 3 seconds.

CONFIRMED BY TESTING (2026-09): this relay module actually energizes on
GPIO HIGH, not LOW as the "low-level trigger" jumper label suggested -
either the jumper is set the other way, or this board just behaves this
way. Logic below matches what was actually observed on the hardware:
  - GPIO HIGH -> relay energized     (ON  - contacts closed, relay LED lit,
                                       you should hear/feel a click)
  - GPIO LOW  -> relay de-energized  (OFF - contacts open)

You do NOT need the actual roof motor connected to run this test - just
watch/listen to the relay module itself for the click and its onboard LED.

Starts in the OFF (LOW) state for safety, then flips ON/OFF every 3
seconds so you can confirm it switches cleanly both ways.

Run with:
    python3 test_relay.py
Stop with Ctrl+C (this leaves the relay OFF on exit).
"""
import time

import RPi.GPIO as GPIO

RELAY_PIN = 23  # GPIO23 / header pin 16 - active HIGH on this module

GPIO.setmode(GPIO.BCM)
GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)  # LOW = OFF at startup, for safety

print("Toggling roof relay on GPIO23 - Ctrl+C to stop (leaves relay OFF)")

try:
    state_on = False
    while True:
        state_on = not state_on
        GPIO.output(RELAY_PIN, GPIO.HIGH if state_on else GPIO.LOW)
        print("Relay: ON  (energized)" if state_on else "Relay: OFF (de-energized)")
        time.sleep(3)

except KeyboardInterrupt:
    print("\nStopped - forcing relay OFF.")

finally:
    GPIO.output(RELAY_PIN, GPIO.LOW)  # make sure we leave it de-energized
    GPIO.cleanup()
