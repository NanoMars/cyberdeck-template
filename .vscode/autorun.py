#!/usr/bin/env python3
"""Sends main.py to the simulated board whenever the simulator starts.

Press "Start the simulation" in the Wokwi tab and your code is on the board a
moment later. No task to remember, no command to type.

How it works: the Wokwi extension opens a serial server on port 4000 when the
simulation starts. This watches that port. When it opens, it copies main.py to
the board and runs it on the same connection, so everything the code prints
appears here from its very first line. When the simulation stops the port
closes, and this arms itself again for the next run.

Deliberately not clever. It polls a port rather than hooking Wokwi's own
commands, because those command IDs are not documented and could change.
"""

import atexit
import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

PORT = int(os.environ.get("WOKWI_SERIAL_PORT", "4000"))
HOST = "127.0.0.1"
SCRIPT = os.environ.get("CYBERDECK_MAIN", "main.py")
DEVICE = f"port:rfc2217://localhost:{PORT}"
VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv")
# Binding this port is how a second copy of the watcher notices the first.
LOCK_PORT = int(os.environ.get("CYBERDECK_LOCK_PORT", "47321"))

# MicroPython needs a moment after the port opens before it will answer.
# Six tries over about eight seconds: long enough for a slow boot, short
# enough that a real failure is reported while the participant is still
# looking at the terminal.
BOOT_ATTEMPTS = 6
BOOT_BACKOFF = 0.5
# mpremote blocks if the simulation is paused, so cap each attempt.
ATTEMPT_TIMEOUT = 20
# How long to let the serial server introduce itself before deciding that
# whatever accepted the connection is not the simulator.
HANDSHAKE_TIMEOUT = 1.0


SERIAL_LOCK = os.path.join(tempfile.gettempdir(), "cyberdeck-serial.lock")


@contextlib.contextmanager
def serial_lock():
    """Only one thing may talk to the board at a time.

    Two mpremote sessions on the same serial line interleave and corrupt each
    other's raw-REPL protocol, which shows up as stray bytes and ENOENT errors
    that have nothing to do with your code. The watcher and the manual tasks
    both take this, so they queue instead of colliding.
    """
    if fcntl is None:
        yield
        return
    handle = open(SERIAL_LOCK, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def python_with_mpremote() -> str:
    """Return an interpreter that can run mpremote, creating one if need be.

    In a Codespace the dev container already installed it. Locally there is
    usually nothing, and the system Python on macOS refuses installs anyway.
    So fall back to a project .venv and set it up once, rather than making
    somebody read an error and go hunting.
    """
    if _has_mpremote(sys.executable):
        return sys.executable

    venv_python = os.path.join(VENV, "bin", "python")
    if os.name == "nt":
        venv_python = os.path.join(VENV, "Scripts", "python.exe")
    if os.path.exists(venv_python) and _has_mpremote(venv_python):
        return venv_python

    print("Setting up mpremote, the tool that talks to the board. Once only.", flush=True)
    if not os.path.exists(venv_python):
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    result = subprocess.run(
        [venv_python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "mpremote>=1.24"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not _has_mpremote(venv_python):
        sys.stderr.write(result.stderr or result.stdout)
        sys.stderr.flush()
        print("\n  Could not install mpremote. Run this yourself and try again:", flush=True)
        print(f"    {sys.executable} -m venv .venv && .venv/bin/pip install mpremote", flush=True)
        sys.exit(1)
    print("Ready.", flush=True)
    return venv_python


def _has_mpremote(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", "import mpremote"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def claim_single_instance() -> bool:
    """Only one watcher at a time.

    In a Codespace this is started by the dev container, and locally by the
    folder-open task. If both fire, the second should bow out rather than
    fight the first for the serial port.
    """
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        lock.bind((HOST, LOCK_PORT))
    except OSError:
        lock.close()
        return False
    lock.listen(1)
    atexit.register(lock.close)
    globals()["_lock"] = lock  # keep it alive for the process lifetime
    return True


def port_is_open() -> bool:
    """True when something is listening AND it behaves like the simulator.

    A plain connect is not enough. A port forwarder accepts the connection and
    then never says anything, which looks identical to a running board until
    mpremote blocks on it for two minutes. Wokwi's RFC2217 server starts the
    telnet negotiation as soon as a client connects, so a socket that accepts
    and then stays silent is not the simulator.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            if sock.connect_ex((HOST, PORT)) != 0:
                return False

            # IAC (0xff) opens every telnet option negotiation. The real server
            # sends one straight away; a forwarder sends nothing and times out.
            sock.settimeout(HANDSHAKE_TIMEOUT)
            try:
                return sock.recv(1) == b"\xff"
            except (TimeoutError, socket.timeout):
                return False
    except OSError:
        return False


def send() -> bool:
    """Copy the script to the board and run it, streaming its output here.

    Deliberately one mpremote session, holding the serial lock throughout.

    The obvious version - copy, soft-reset, then attach separately - loses the
    beginning of the output. A soft reset starts main.py immediately, so
    anything it prints in its first moments goes out on the serial line while
    nothing is connected, and the reader that attaches afterwards has already
    missed it. `run` executes the script on a connection that is already open,
    so the very first print arrives.
    """
    for attempt in range(1, BOOT_ATTEMPTS + 1):
        try:
            with serial_lock():
                # Copy quietly first. This is also the connection test: if the
                # board is not answering yet, fail here rather than half way
                # through handing the terminal over.
                copied = subprocess.run(
                    [PYTHON, "-m", "mpremote", "connect", DEVICE,
                     "fs", "cp", SCRIPT, f":{SCRIPT}"],
                    capture_output=True, text=True, timeout=ATTEMPT_TIMEOUT,
                )
                if copied.returncode == 0:
                    print(f"{SCRIPT} is running. Its output appears below.", flush=True)
                    print("-" * 60, flush=True)
                    # Not captured, so the board's output lands in this
                    # terminal as it happens, from the first line onwards.
                    subprocess.run(
                        [PYTHON, "-m", "mpremote", "connect", DEVICE, "run", SCRIPT]
                    )
                    print("-" * 60, flush=True)
                    return True
                result = copied
        except subprocess.TimeoutExpired:
            result = None
        except KeyboardInterrupt:
            raise
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
            print("\n  Could not reach the board.", flush=True)
            print("  Almost always this: the Wokwi tab is not the visible tab.", flush=True)
            print("  Wokwi pauses the simulation when its tab is hidden, so the", flush=True)
            print("  board stops answering. Click the Wokwi tab and try again.", flush=True)
            return False
        time.sleep(BOOT_BACKOFF * attempt)
    return False


def send_once() -> int:
    """Send the code now, without waiting for anything."""
    if not port_is_open():
        print("The simulator is not running. Press Start in the Wokwi tab first.", flush=True)
        return 1
    return 0 if send() else 1


def repl() -> int:
    """Hand the terminal straight to the board."""
    if not port_is_open():
        print("The simulator is not running. Press Start in the Wokwi tab first.", flush=True)
        return 1
    with serial_lock():
        return subprocess.run([PYTHON, "-m", "mpremote", "connect", DEVICE, "repl"]).returncode


def main() -> None:
    if not claim_single_instance():
        print("Another watcher is already running. Nothing to do here.", flush=True)
        return

    print(f"Watching for the simulator on port {PORT}. Press Start in the Wokwi tab.", flush=True)
    while True:
        while not port_is_open():
            time.sleep(0.5)

        print(f"\nSimulator running. Sending {SCRIPT} ...", flush=True)
        send()

        # Hold here until the simulation stops, so the next Start re-sends.
        while port_is_open():
            time.sleep(1)
        print("Simulator stopped. Waiting for the next run.", flush=True)


if __name__ == "__main__":
    PYTHON = python_with_mpremote()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--watch"
    try:
        if mode == "--once":
            sys.exit(send_once())
        elif mode == "--repl":
            sys.exit(repl())
        else:
            main()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
