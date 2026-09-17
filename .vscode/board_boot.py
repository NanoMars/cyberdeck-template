# The watcher copies this file to the board as _cyberdeck.py, every time it
# runs your code, and writes a boot.py that imports it. Do not put your
# project here. Everything of yours lives in main.py. If you want your own
# boot.py, write one next to main.py: the watcher runs it after this file.
#
# What it does: twice a second it writes a short invisible marker to the
# serial line. The watcher in .vscode/autorun.py strips the marker before it
# shows you anything, and uses it to notice when the board has been restarted
# from the Wokwi tab. Without it there is no way to tell from outside the
# simulator, which was measured on 2026-09-17.
#
# The marker carries a random id chosen at boot, so a restarted board looks
# different from a paused one.
import machine
import random
import sys

_cd_id = "%04x" % random.getrandbits(16)


def _cd_heartbeat(_timer):
    sys.stdout.write("\x1e" + _cd_id + "\x1f")


_cd_timer = machine.Timer(period=500, mode=machine.Timer.PERIODIC, callback=_cd_heartbeat)
