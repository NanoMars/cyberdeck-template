# The watcher copies this file to the board as _cyberdeck.py every time it
# runs your code, and writes a boot.py that clears the screen and imports it.
# Do not put your project here. Everything of yours lives in main.py. If you
# want your own boot.py, write one next to main.py: it runs after this.
#
# What it does: twice a second it writes a short marker to the serial line.
# The marker is made of control characters that terminals do not draw, so
# you never see it in the Wokwi Terminal. The watcher in .vscode/autorun.py
# listens for it. When it stops, the board was restarted or stopped from the
# Wokwi tab, and the watcher sends your code again if the board came back
# empty. Without the marker there is no way to tell from outside the
# simulator. Measured on 2026-09-17.
#
# The marker carries a random id chosen at boot, so a restarted board looks
# different from a paused one.
import machine
import random
import sys

_T = "\x01\x02\x03\x05\x06\x10\x12\x14\x15\x16\x17\x18\x19\x1a\x1c\x1d"
_n = random.getrandbits(16)
_cd_id = "".join(_T[(_n >> s) & 15] for s in (12, 8, 4, 0))


def _cd_heartbeat(_timer):
    sys.stdout.write("\x1e" + _cd_id + "\x1f")


_cd_timer = machine.Timer(period=500, mode=machine.Timer.PERIODIC, callback=_cd_heartbeat)
