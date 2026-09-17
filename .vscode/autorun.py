#!/usr/bin/env python3
"""Sends main.py to the simulated board whenever the simulator starts.

Press "Start the simulation" in the Wokwi tab and your code is on the board a
moment later. No task to remember, no command to type.

How it works: the Wokwi extension opens a serial server on port 47322 the
first time the simulation starts. This watches that port. When it opens, it
copies main.py to the board and runs it on the same connection, so everything
the code prints appears here from its very first line.

What it cannot do: notice a restart. Measured on 2026-09-17 in a Codespace,
the serial port stays open after Stop, for the life of the extension. A client
that was attached before a Restart or a Stop/Start is left connected but hears
nothing more, and nothing else in the container changes either: no log line,
no file read, no new socket. So after you restart the simulation in Wokwi,
press Enter here. That kills the dead connection, opens a fresh one to the new
board, and sends main.py again. The "Send code now" task does the same thing.

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
import threading
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

def _port_from_wokwi_toml(default: int) -> int:
    """Read the port out of wokwi.toml so the two files cannot disagree.

    Wokwi owns this number: it is the server, this is the client. Hard-coding
    it in both places means a change to one silently breaks the other, and the
    symptom is a watcher that waits forever for a port nobody opened.
    """
    toml = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wokwi.toml")
    try:
        with open(toml) as handle:
            for line in handle:
                line = line.split("#", 1)[0].strip()
                if line.startswith("rfc2217ServerPort"):
                    return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return default


# Not 4000: too crowded a default, and in a Codespace something else had it.
PORT = int(os.environ.get("WOKWI_SERIAL_PORT") or _port_from_wokwi_toml(47322))
HOST = "127.0.0.1"
SCRIPT = os.environ.get("CYBERDECK_MAIN", "main.py")
DEVICE = f"port:rfc2217://localhost:{PORT}"
VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv")
# Binding this port is how a second copy of the watcher notices the first.
# Connecting to it is how "Send code now" asks the running watcher to re-send.
LOCK_PORT = int(os.environ.get("CYBERDECK_LOCK_PORT", "47321"))
# How often to try again while the board is not answering.
RETRY_EVERY = 2

# MicroPython needs a moment after the port opens before it will answer.
# Six tries over about eight seconds: long enough for a slow boot, short
# enough that a real failure is reported while the participant is still
# looking at the terminal.
BOOT_ATTEMPTS = 6
BOOT_BACKOFF = 0.5
# mpremote blocks if the simulation is paused, so cap each attempt.
ATTEMPT_TIMEOUT = 20


SERIAL_LOCK = os.path.join(tempfile.gettempdir(), "cyberdeck-serial.lock")

# The mpremote process that currently holds the board, if any, and the flag
# that says somebody asked for main.py to be sent again.
_child = None
_resend = threading.Event()


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
    """True when something is listening on the serial port.

    A plain connect, deliberately. An earlier version also waited for the
    server to send a telnet IAC byte, to tell a real serial server from a port
    forwarder that accepts and then says nothing. That was wrong: Wokwi's
    server waits for the client to speak first, so the check rejected the very
    thing it was meant to detect and the watcher never sent any code.

    The forwarder problem is solved where it belongs, by not sitting on a port
    anything else wants: the serial port is not forwarded, and it is 47322
    rather than a crowded default. If a connect succeeds here, it is Wokwi.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((HOST, PORT)) == 0
    except OSError:
        return False


def send(verbose: bool = True) -> bool:
    """Copy the script to the board and run it, streaming its output here.

    Deliberately one mpremote session, holding the serial lock throughout.

    The obvious version - copy, soft-reset, then attach separately - loses the
    beginning of the output. A soft reset starts main.py immediately, so
    anything it prints in its first moments goes out on the serial line while
    nothing is connected, and the reader that attaches afterwards has already
    missed it. `run` executes the script on a connection that is already open,
    so the very first print arrives.

    Returns when the script ends, when the board cannot be reached, or when
    somebody asks for a re-send and the running mpremote is killed.
    """
    global _child
    for attempt in range(1, BOOT_ATTEMPTS + 1):
        if _resend.is_set():
            return False
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
                    print("Edit it and press Enter here to run it again. "
                          "Do the same after a Restart in Wokwi.", flush=True)
                    print("-" * 60, flush=True)
                    # Not captured, so the board's output lands in this
                    # terminal as it happens, from the first line onwards.
                    # stdin is closed off so that Enter reaches the watcher,
                    # not mpremote.
                    _child = subprocess.Popen(
                        [PYTHON, "-m", "mpremote", "connect", DEVICE, "run", SCRIPT],
                        stdin=subprocess.DEVNULL,
                    )
                    try:
                        _child.wait()
                    finally:
                        _child = None
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
        # "could not enter raw repl" means the board is still booting, the
        # simulation is stopped, or the Wokwi tab is hidden, which pauses the
        # simulation entirely.
        if attempt == BOOT_ATTEMPTS:
            if verbose:
                if result is not None:
                    sys.stderr.write(result.stderr or result.stdout)
                sys.stderr.flush()
                print("\n  Could not reach the board.", flush=True)
                print("  Almost always this: the Wokwi tab is not the visible tab, or", flush=True)
                print("  the simulation is stopped. Wokwi pauses the board when its", flush=True)
                print("  tab is hidden. Click the Wokwi tab, make sure it is running.", flush=True)
                print(f"  Trying again every {RETRY_EVERY} seconds ...", flush=True)
            return False
        time.sleep(BOOT_BACKOFF * attempt)
    return False


def request_resend(source: str) -> None:
    """Ask the main loop to send main.py again, interrupting a running script.

    Killing the mpremote that holds the board is the whole point. After a
    Restart in Wokwi that process is attached to a board that no longer
    exists and will never return on its own.
    """
    _resend.set()
    child = _child
    if child is not None:
        with contextlib.suppress(OSError):
            child.terminate()
    print(f"\n{source}: sending {SCRIPT} again ...", flush=True)


def _watch_stdin() -> None:
    """Enter in this terminal means: send the code again."""
    try:
        for _ in sys.stdin:
            request_resend("Enter pressed")
    except (OSError, ValueError):
        pass


def _watch_lock_port(lock: socket.socket) -> None:
    """A connection to the lock port means: send the code again.

    This is how the "Send code now" task reaches a watcher that already owns
    the board, instead of fighting it for the serial lock.
    """
    while True:
        try:
            conn, _ = lock.accept()
        except OSError:
            return
        conn.close()
        request_resend("Send code now")


def _watcher_is_running() -> bool:
    try:
        with socket.create_connection((HOST, LOCK_PORT), timeout=0.3):
            return True
    except OSError:
        return False


def send_once() -> int:
    """Send the code now, without waiting for anything.

    If a watcher is running, hand the request to it, so the output lands in
    "Board output" like every other run. Connecting to the lock port is the
    request; the watcher does the rest.
    """
    if not port_is_open():
        print("The simulator is not running. Press Start in the Wokwi tab first.", flush=True)
        return 1
    if _watcher_is_running():
        print('Asked the watcher to send main.py again. Look in "Board output".', flush=True)
        return 0
    return 0 if send() else 1


def repl() -> int:
    """Hand the terminal straight to the board."""
    if not port_is_open():
        print("The simulator is not running. Press Start in the Wokwi tab first.", flush=True)
        return 1
    with serial_lock():
        return subprocess.run([PYTHON, "-m", "mpremote", "connect", DEVICE, "repl"]).returncode


def main() -> None:
    # The pid is here because VS Code can start this task more than once while
    # a Codespace settles, and without it there is no way to tell one watcher
    # restarting from several fighting.
    if not claim_single_instance():
        print(f"[pid {os.getpid()}] Another watcher has the board. Nothing to do here.", flush=True)
        return

    print(f"[pid {os.getpid()}] Watching for the simulator on port {PORT}. "
          f"Press Start in the Wokwi tab.", flush=True)
    # Wokwi opens a terminal of its own, and it stays empty because the board's
    # serial goes to this script over RFC2217 instead. Say so here, or the
    # first thing a participant does is watch the wrong terminal.
    print('Your code prints here, in "Board output". Wokwi\'s own terminal stays empty.',
          flush=True)
    threading.Thread(target=_watch_lock_port, args=(globals()["_lock"],), daemon=True).start()
    if sys.stdin.isatty():
        threading.Thread(target=_watch_stdin, daemon=True).start()

    while True:
        while not port_is_open():
            time.sleep(0.5)

        print(f"\nSimulator running. Sending {SCRIPT} ...", flush=True)
        verbose = True
        while True:
            _resend.clear()
            ok = send(verbose=verbose)
            if _resend.is_set():
                # Somebody pressed Enter or ran the task. Go straight back in.
                verbose = True
                continue
            if ok:
                # The script ended on its own. Wait to be asked again. The
                # port does not close when the simulation stops, so there is
                # nothing else to wait for.
                print(f"{SCRIPT} finished. Press Enter to run it again.", flush=True)
                _resend.wait()
                verbose = True
                continue
            if not port_is_open():
                print("Simulator gone. Waiting for it to come back.", flush=True)
                break
            # Board not answering: stopped, hidden, or still booting. Say so
            # once, then keep trying quietly until it answers.
            verbose = False
            if _resend.wait(RETRY_EVERY):
                verbose = True


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
