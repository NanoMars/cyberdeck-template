#!/usr/bin/env python3
"""Runs main.py on the simulated board every time you save it.

Read your program's output in the Wokwi Terminal, the terminal Wokwi opens
when the simulation starts. It is also a MicroPython prompt: Ctrl-C stops
your program, and you can type Python straight at the board. This terminal
only says what the watcher did, and what went wrong.

How it works. Wokwi opens a serial server on port 47322 the first time the
simulation starts. On every save this connects to it, speaks MicroPython's
raw REPL protocol, copies main.py and a small boot.py to the board's flash,
and soft resets the board. MicroPython then runs boot.py and main.py. The
template's boot.py first clears the Wokwi Terminal, so the transfer chatter
and the MicroPython banner are gone before your program prints its first
line. Save, and the terminal shows only your output.

Restart in the Wokwi tab. Sometimes the flash survives it and main.py runs
again by itself. Sometimes the board comes back empty. Both were measured
on 2026-09-17. So the board's boot.py writes an invisible marker twice a
second, this listens for it, and when it stops and the board turns out to be
empty, the files are sent again.

Why not mpremote. Its handshake breaks on the marker bytes, and its prompt
does not work over RFC2217. pyserial is the only dependency.
"""

import atexit
import contextlib
import hashlib
import os
import re
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _port_from_wokwi_toml(default: int) -> int:
    """Read the port out of wokwi.toml so the two files cannot disagree."""
    try:
        with open(os.path.join(ROOT, "wokwi.toml")) as handle:
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
MARKER_SOURCE = os.path.join(ROOT, ".vscode", "board_boot.py")
# A participant's own boot.py, if they wrote one. It runs after ours.
USER_BOOT = os.path.join(ROOT, "boot.py")
DEVICE_URL = f"rfc2217://{HOST}:{PORT}"
VENV = os.path.join(ROOT, ".venv")
# Binding this port is how a second copy of the watcher notices the first.
# Connecting to it and sending a line is how the tasks talk to the watcher.
LOCK_PORT = int(os.environ.get("CYBERDECK_LOCK_PORT", "47321"))

# The marker boot.py writes: \x1e, four control bytes, \x1f. Same table as
# board_boot.py. None of these bytes is drawn by a terminal.
MARKER = re.compile(rb"\x1e([\x01\x02\x03\x05\x06\x10\x12\x14\x15\x16\x17\x18\x19\x1a\x1c\x1d]{4})\x1f")
# How long without a marker before the board is presumed gone.
SILENCE = 1.5
# How often to look again while the board is not answering.
CHECK_EVERY = 2.0
# How long to keep quiet about a board that does not answer. It takes a
# moment to boot after Start, and that is not worth a warning.
PATIENCE = 10.0
# How often to look at main.py for a save.
POLL_FILES = 0.3
# What the board prints first at every boot, so the Wokwi Terminal shows
# only the program's own output.
CLEAR_SCREEN = b'print("\\x1b[2J\\x1b[H", end="")\n'

# ---------------------------------------------------------------------------
# Dependencies. pyserial is what talks RFC2217. In a Codespace the dev
# container already installed it. Locally there may be nothing, so fall back
# to a project .venv and re-run from there.


def ensure_dependencies() -> None:
    try:
        import serial  # noqa: F401
        return
    except ImportError:
        pass
    venv_python = os.path.join(VENV, "bin", "python")
    if os.name == "nt":
        venv_python = os.path.join(VENV, "Scripts", "python.exe")
    if os.path.abspath(sys.executable) == os.path.abspath(venv_python):
        print("Could not import pyserial even inside .venv. Run this yourself and try again:", flush=True)
        print(f"    {venv_python} -m pip install pyserial", flush=True)
        sys.exit(1)
    if not os.path.exists(venv_python):
        print("Setting up the tool that talks to the board. Once only.", flush=True)
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    result = subprocess.run(
        [venv_python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "pyserial>=3.5"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
        sys.exit(1)
    os.execv(venv_python, [venv_python] + sys.argv)


# ---------------------------------------------------------------------------
# The board.


class BoardGone(Exception):
    """The board did not answer in time."""


class Board:
    """One connection to the simulated board over RFC2217.

    Every byte read passes through strip(), which removes the boot.py marker
    and records when it was last seen. Protocol matching works on the
    stripped stream. Nothing else is done with the board's output: the
    participant reads it in the Wokwi Terminal.
    """

    def __init__(self):
        import serial
        self.ser = serial.serial_for_url(DEVICE_URL, timeout=0.05, do_not_open=True)
        self.ser.open()
        self.tail = b""
        self.tail_since = 0.0
        # Bytes read past a terminator by read_until, handed out first next time.
        self.pending = b""
        self.last_marker = time.monotonic()
        self.marker_id = None
        self.seen_marker = False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.ser.close()

    def write(self, data: bytes) -> None:
        self.ser.write(data)

    def strip(self, data: bytes) -> bytes:
        """Remove markers, keeping a possible partial marker for the next read."""
        data = self.tail + data
        self.tail = b""

        def swallow(match):
            self.last_marker = time.monotonic()
            self.marker_id = match.group(1)
            self.seen_marker = True
            return b""

        data = MARKER.sub(swallow, data)
        cut = data.rfind(b"\x1e")
        if cut != -1 and cut >= len(data) - 6 and b"\x1f" not in data[cut:]:
            self.tail = data[cut:]
            self.tail_since = time.monotonic()
            data = data[:cut]
        return data

    def read(self) -> bytes:
        """Read whatever is there, stripped. Returns b"" on nothing."""
        if self.pending:
            data, self.pending = self.pending, b""
            return data
        data = self.ser.read(4096)
        if data:
            return self.strip(data)
        if self.tail and time.monotonic() - self.tail_since > 0.3:
            data, self.tail = self.tail, b""
            return data
        return b""

    def drain(self, seconds: float) -> bytes:
        end = time.monotonic() + seconds
        out = b""
        while time.monotonic() < end:
            out += self.read()
        return out

    def read_until(self, ending: bytes, timeout: float) -> bytes:
        """Read up to and including ending. Anything after it waits in pending.

        The reply to one raw REPL command often arrives in a single chunk, so
        the terminator is rarely the last thing read. Search, do not endswith.
        """
        data = b""
        end = time.monotonic() + timeout
        while True:
            cut = data.find(ending)
            if cut != -1:
                cut += len(ending)
                self.pending = data[cut:] + self.pending
                return data[:cut]
            if time.monotonic() > end:
                raise BoardGone(f"waited {timeout}s for {ending!r}, got {data[-80:]!r}")
            data += self.read()

    # Raw REPL. The protocol is MicroPython's: Ctrl-A enters it, code then
    # Ctrl-D runs it, the reply is "OK", stdout, Ctrl-D, stderr, Ctrl-D, ">".

    def enter_raw(self) -> None:
        self.write(b"\r\x03\x03")
        self.drain(0.15)
        self.write(b"\r\x01")
        self.read_until(b"raw REPL; CTRL-B to exit\r\n>", 5)

    def exec_raw(self, code: bytes, timeout: float = 10) -> bytes:
        for start in range(0, len(code), 256):
            self.write(code[start:start + 256])
            time.sleep(0.005)
        self.write(b"\x04")
        self.read_until(b"OK", timeout)
        out = self.read_until(b"\x04", timeout)[:-1]
        err = self.read_until(b"\x04", timeout)[:-1]
        self.read_until(b">", timeout)
        if err:
            raise BoardGone(err.decode(errors="replace"))
        return out

    def write_file(self, name: str, data: bytes) -> None:
        # One exec per file: one "OK" of chatter in the Wokwi Terminal, not
        # one per chunk. boot.py clears it anyway.
        self.exec_raw(f"f=open({name!r},'wb');f.write({data!r});f.close()".encode())

    def soft_reset(self) -> None:
        """Leave raw REPL, then soft reset. MicroPython then runs main.py."""
        self.write(b"\x02")
        self.drain(0.15)
        self.write(b"\x04")
        # Boot takes about a second before the first marker. Not silence.
        self.last_marker = time.monotonic() + 1.5
        self.seen_marker = False


def port_is_open() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((HOST, PORT)) == 0
    except OSError:
        return False


def probe_board(seconds: float = 1.2):
    """Open a second connection and listen. Returns a marker id, "bare", or None.

    "bare" means a board that answers the prompt but runs no boot.py of
    ours: a fresh simulation, or a Restart that wiped the flash. None means
    nothing answered: the simulation is stopped, or paused behind a hidden
    tab. The newline sent to tell those apart echoes one prompt in the Wokwi
    Terminal, which is why it is only sent when the marker has stopped.
    """
    try:
        probe = Board()
    except Exception:
        return None
    try:
        probe.drain(seconds)
        if probe.seen_marker:
            return probe.marker_id
        probe.write(b"\r\n")
        if b">>>" in probe.drain(0.5):
            return "bare"
        return None
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# The watcher.

_rerun = threading.Event()


def say(text: str) -> None:
    print(text, flush=True)


def file_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def sources_digest() -> str:
    parts = (file_bytes(os.path.join(ROOT, SCRIPT)), file_bytes(MARKER_SOURCE), file_bytes(USER_BOOT))
    return hashlib.sha1(b"\0".join(parts)).hexdigest()


def board_boot_py() -> bytes:
    """The boot.py the board gets: clear the screen, our marker, then theirs.

    MicroPython runs boot.py before main.py at every boot. The clear wipes
    the transfer chatter and the banner from the Wokwi Terminal. The marker
    must start. A participant's own boot.py from the project folder follows.
    """
    ours = CLEAR_SCREEN + b"import _cyberdeck\n"
    theirs = file_bytes(USER_BOOT)
    if not theirs.strip():
        return ours
    return ours + b"# --- your boot.py, copied from the project folder ---\n" + theirs


def run_code(board: Board) -> None:
    """Copy the files to the board and soft reset it."""
    board.enter_raw()
    board.write_file("_cyberdeck.py", file_bytes(MARKER_SOURCE))
    board.write_file("boot.py", board_boot_py())
    board.write_file(SCRIPT, file_bytes(os.path.join(ROOT, SCRIPT)))
    board.soft_reset()


def connect_and_run(why: str) -> Board:
    """Connect, copy, reset. Keeps trying while the board does not answer."""
    started = time.monotonic()
    hinted = False
    while True:
        if not port_is_open():
            raise BoardGone("port closed")
        board = None
        try:
            board = Board()
            run_code(board)
            say(f"{SCRIPT} sent, {why}. Its output is in the Wokwi Terminal.")
            return board
        except (BoardGone, OSError):
            if board is not None:
                board.close()
            if not hinted and time.monotonic() - started > PATIENCE:
                say("\n  The board is not answering.")
                say("  Almost always this: the Wokwi tab is not the visible tab, or the")
                say("  simulation is stopped. Wokwi pauses a hidden tab. Click the Wokwi")
                say(f"  tab and check it is running. Trying again every {CHECK_EVERY:g} seconds.")
                hinted = True
            if _rerun.wait(CHECK_EVERY):
                _rerun.clear()


def watch_board(board: Board) -> str:
    """Listen until something needs a new run. Returns why."""
    digest = sources_digest()
    next_poll = time.monotonic() + POLL_FILES
    quiet_said = False
    next_check = 0.0
    while True:
        if _rerun.is_set():
            _rerun.clear()
            return "run again"

        board.read()  # keeps the marker time fresh; the bytes themselves are not needed

        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL_FILES
            if sources_digest() != digest:
                time.sleep(0.2)  # let the editor finish writing
                return "saved"

        silent = now - board.last_marker > SILENCE
        if not silent:
            if quiet_said:
                say("  Running again.")
                quiet_said = False
            continue
        if now < next_check:
            continue
        next_check = now + CHECK_EVERY
        if not port_is_open():
            return "port closed"
        verdict = probe_board()
        if verdict is None:
            if not quiet_said:
                say("\n  The simulation is not running. Is it stopped, or is the Wokwi tab")
                say("  hidden? Wokwi pauses a hidden tab. Waiting for it.")
                quiet_said = True
            continue
        if verdict == "bare":
            return "started in Wokwi" if quiet_said else "restarted in Wokwi"
        if verdict != board.marker_id:
            # A new id: the board rebooted with our files still on it, and is
            # already running main.py by itself. Only the connection is dead.
            return "reconnected"
        return "reconnected"


def control_thread(lock: socket.socket) -> None:
    """Tasks connect to the lock port and send one line: send or quit."""
    while True:
        try:
            conn, _ = lock.accept()
        except OSError:
            return
        try:
            conn.settimeout(2)
            line = conn.makefile("rb").readline().strip()
        except OSError:
            conn.close()
            continue
        if line == b"quit":
            # A newer copy of this script is taking over. It is the one in
            # the terminal the participant can see.
            with contextlib.suppress(OSError):
                conn.sendall(b"ok\n")
            conn.close()
            say("\nA newer cyberdeck terminal took over. This one is a plain shell now.")
            os._exit(0)
        _rerun.set()
        with contextlib.suppress(OSError):
            conn.sendall(b"ok\n")
        conn.close()


def stdin_thread() -> None:
    """Enter in this terminal runs main.py again. Not advertised, but handy."""
    try:
        for _ in sys.stdin:
            _rerun.set()
    except (OSError, ValueError):
        pass


def claim_single_instance():
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind((HOST, LOCK_PORT))
    except OSError:
        lock.close()
        return None
    lock.listen(2)
    atexit.register(lock.close)
    return lock


def ask_watcher(line: bytes):
    """Send one line to a running watcher. Returns the socket, or None."""
    try:
        conn = socket.create_connection((HOST, LOCK_PORT), timeout=2)
        conn.sendall(line + b"\n")
        conn.settimeout(15)
        conn.makefile("rb").readline()
        return conn
    except OSError:
        return None


def watch() -> None:
    lock = claim_single_instance()
    if lock is None:
        # Another copy is running, in a terminal VS Code has probably replaced
        # when the window reconnected. The newest terminal is the one the
        # participant sees, so this copy takes over.
        ask_watcher(b"quit")
        for _ in range(50):
            time.sleep(0.1)
            lock = claim_single_instance()
            if lock is not None:
                break
        if lock is None:
            say(f"[pid {os.getpid()}] Another cyberdeck terminal has the board and did not let go.")
            return
    say(f"[pid {os.getpid()}] cyberdeck. Save {SCRIPT} (Cmd+S or Ctrl+S) and it runs on the board.")
    say("Read its output in the Wokwi Terminal. This terminal only reports what happened.")
    threading.Thread(target=control_thread, args=(lock,), daemon=True).start()
    if sys.stdin.isatty():
        threading.Thread(target=stdin_thread, daemon=True).start()

    while True:
        if not port_is_open():
            say(f"\nWaiting for the simulator on port {PORT}. Start it from the Wokwi tab,")
            say("or press F1 and run \"Wokwi: Start Simulator\".")
            while not port_is_open():
                time.sleep(0.5)
        why = "simulator started"
        board = None
        while True:
            try:
                if board is None:
                    board = connect_and_run(why)
                why = watch_board(board)
            except BoardGone as error:
                if str(error) == "port closed":
                    break
                why = "reconnected"
            except OSError:
                why = "reconnected"
            if why == "port closed":
                break
            if board is not None:
                board.close()
                board = None
            if why == "reconnected":
                # Attach again without a reset. The board is running by itself.
                try:
                    board = Board()
                except Exception:
                    pass
                continue
            # saved, run again, started or restarted in Wokwi: copy and reset.
        if board is not None:
            board.close()
        say("\nThe simulator is gone. Waiting for the next one.")


def send_once() -> int:
    if not port_is_open():
        say("The simulator is not running. Press Start in the Wokwi tab first.")
        return 1
    conn = ask_watcher(b"send")
    if conn is not None:
        conn.close()
        say(f"Asked the watcher to run {SCRIPT} again. Read the Wokwi Terminal.")
        return 0
    connect_and_run("sent by hand").close()
    return 0


if __name__ == "__main__":
    ensure_dependencies()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--watch"
    try:
        sys.exit(send_once()) if mode == "--once" else watch()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
