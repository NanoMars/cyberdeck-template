"""Your cyberdeck starts here.

The board in diagram.json is a Raspberry Pi Pico with an LED on GP15 and a
button on GP14. Press "Run on Wokwi" in the editor to see it work, then change
whatever you like. Adding parts to the diagram is part of the fun.
"""

from machine import Pin
import time

led = Pin(15, Pin.OUT)
button = Pin(14, Pin.IN, Pin.PULL_UP)

print("cyberdeck online")

while True:
    # The button pulls the pin low when pressed, so a press reads as 0.
    if button.value() == 0:
        led.toggle()
        print("button pressed, led is now", "on" if led.value() else "off")
        # Crude debounce. Replace it with something better when it annoys you.
        time.sleep(0.25)
    time.sleep(0.01)
