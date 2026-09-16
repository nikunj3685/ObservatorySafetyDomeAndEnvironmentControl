#!/usr/bin/env python3
"""
Quick test: toggle the IRF520 MOSFET driver on GPIO18 on and off every 3
seconds.

SAFETY FIRST: before running this with the real heater element connected to
the IRF520's OUT/V+/V- screw terminals, test it with something low-stakes
instead - e.g. a spare LED (with a resistor, ~220-330 ohm) wired across
OUT and V-, and V+ fed from a small supply (or even a couple AA batteries).
Confirm the LED blinks on/off in step with this script before ever wiring
up the actual heater.

Most IRF520 breakout boards are active-HIGH on SIG:
  - GPIO HIGH -> MOSFET conducts -> load ON  (LED lit / heater powered)
  - GPIO LOW  -> MOSFET off      -> load OFF

If you see the same inverted behavior we hit with the relay (prints ON
when it's actually OFF), swap GPIO.HIGH/GPIO.LOW below the same way we
fixed test_relay.py - tell me and I'll send the corrected version.

Starts in the OFF (LOW) state for safety, then flips ON/OFF every 3
seconds so you can confirm it switches cleanly both ways.

Run with:
    python3 test_mosfet.py
Stop with Ctrl+C (this leaves the output OFF on exit).
"""
import time

import RPi.GPIO as GPIO

MOSFET_PIN = 18  # GPIO18 / header pin 12 - hardware PWM capable, digital on/off for this test

GPIO.setmode(GPIO.BCM)
GPIO.setup(MOSFET_PIN, GPIO.OUT, initial=GPIO.LOW)  # LOW = OFF at startup, for safety

print("Toggling IRF520 (heater driver) on GPIO18 - Ctrl+C to stop (leaves output OFF)")

try:
    state_on = False
    while True:
        state_on = not state_on
        GPIO.output(MOSFET_PIN, GPIO.HIGH if state_on else GPIO.LOW)
        print("MOSFET: ON  (load powered)" if state_on else "MOSFET: OFF (load off)")
        time.sleep(3)

except KeyboardInterrupt:
    print("\nStopped - forcing output OFF.")

finally:
    GPIO.output(MOSFET_PIN, GPIO.LOW)  # make sure we leave it off
    GPIO.cleanup()
