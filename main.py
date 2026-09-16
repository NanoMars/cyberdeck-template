from machine import Pin
import time

led = Pin(15, Pin.OUT)
button = Pin(14, Pin.IN, Pin.PULL_UP)

print("Hello hacker!")

while True:
    # The button pulls the pin low when pressed, so a press reads as 0.
    if button.value() == 0:
        led.toggle()
        print("button pressed, led is now", "on" if led.value() else "off")
        time.sleep(0.25)
    time.sleep(0.01)
