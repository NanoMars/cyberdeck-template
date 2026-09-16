#!/usr/bin/env python3
"""Sends main.py to the simulated board whenever the simulator starts.

Press "Start the simulation" in the Wokwi tab and your code is on the board a
moment later. No task to remember, no command to type.

How it works: the Wokwi extension opens a serial server on port 4000 when the
simulation starts. This watches that port. When it opens, it waits for
MicroPython to finish booting, copies main.py across and soft-resets so the
board runs it. When the simulation stops the port closes, and this arms itself
again for the next run.

Deliberately not clever. It polls a port rather than hooking Wokwi's own
commands, because those command IDs are not documented and could change.
"""

import os
import socket
import subprocess
import sys
import time

PORT = int(os.environ.get("WOKWI_SERIAL_PORT", "4000"))
HOST = "127.0.0.1"
SCRIPT = os.environ.get("CYBERDECK_MAIN", "main.py")
DEVICE = f"port:rfc2217://localhost:{PORT}"

# MicroPython needs a moment after the port opens before it will answer.
# Six tries over about eight seconds: long enough for a slow boot, short
# enough that a real failure is reported while the participant is still
# looking at the terminal.
BOOT_ATTEMPTS = 6
BOOT_BACKOFF = 0.5
# mpremote blocks if the simulation is paused, so cap each attempt.
ATTEMPT_TIMEOUT = 20


def port_is_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            return sock.connect_ex((HOST, PORT)) == 0
        except OSError:
            return False


def send() -> bool:
    """Copy the script over and restart the board. True if it landed."""
    for attempt in range(1, BOOT_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                [sys.executable, "-m", "mpremote", "connect", DEVICE,
                 "fs", "cp", SCRIPT, f":{SCRIPT}", "+", "soft-reset"],
                capture_output=True, text=True, timeout=ATTEMPT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            return True
        # The simulation may have been stopped mid-attempt. Do not keep
        # retrying against a port that has gone away.
        if not port_is_open():
            return False
        # "could not enter raw repl" means the board is still booting, or the
        # Wokwi tab is hidden, which pauses the simulation entirely.
        if attempt == BOOT_ATTEMPTS:
            if result is not None:
                sys.stderr.write(result.stderr or result.stdout)
            sys.stderr.flush()
            print("\n  Could not reach the board. Two usual reasons:", flush=True)
            print("  - the Wokwi tab is not visible, which pauses the simulation", flush=True)
            print("  - the simulation was stopped before this finished", flush=True)
            return False
        time.sleep(BOOT_BACKOFF * attempt)
    return False


def main() -> None:
    print(f"Watching for the simulator on port {PORT}. Press Start in the Wokwi tab.", flush=True)
    while True:
        while not port_is_open():
            time.sleep(0.5)

        print(f"\nSimulator running. Sending {SCRIPT} ...", flush=True)
        if send():
            print(f"{SCRIPT} is on the board and running. Output appears in the Wokwi tab.", flush=True)

        # Hold here until the simulation stops, so the next Start re-sends.
        while port_is_open():
            time.sleep(1)
        print("Simulator stopped. Waiting for the next run.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped watching.", flush=True)
