#!/usr/bin/env python3
"""Runs main.py on the simulated board every time you save it.

Press Start in the Wokwi tab once. From then on: edit main.py, save it with
Cmd+S, and it runs. Everything it prints appears in this terminal, from the
first line. Restart in the Wokwi tab works too. The board runs the saved
main.py by itself, and this notices within about two seconds and shows the
output from the start.

How it works. Wokwi opens a serial server on port 47322 the first time the
simulation starts. This connects to it and speaks MicroPython's raw REPL
protocol directly: it copies boot.py and main.py to the board's flash, then
soft resets the board on the same open connection. MicroPython runs main.py
after a soft reset, and because the connection was already open, nothing it
prints is missed.

Why this does not use mpremote. The board's boot.py writes a marker to the
serial line twice a second (see board_boot.py). A connection that was
attached before a Restart hears nothing more, and nothing else in the
container changes, so the marker is the only way to notice. Those marker
bytes break mpremote's exact-match handshake, and mpremote's own prompt does
not work over RFC2217 at all (its console wants a file descriptor). So this
strips the marker itself and offers a prompt of its own. pyserial is the only
dependency.

Measured on 2026-09-17 in a Codespace: flash survives a Restart, several
clients can share the serial port, and a soft reset on an open connection
replays output from the first line.
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
BOOT_SOURCE = os.path.join(ROOT, ".vscode", "board_boot.py")
DEVICE_URL = f"rfc2217://{HOST}:{PORT}"
VENV = os.path.join(ROOT, ".venv")
# Binding this port is how a second copy of the watcher notices the first.
# Connecting to it and sending a line is how the tasks talk to the watcher.
LOCK_PORT = int(os.environ.get("CYBERDECK_LOCK_PORT", "47321"))

# The marker boot.py writes: \x1e, four hex digits, \x1f.
MARKER = re.compile(rb"\x1e([0-9a-f]{4})\x1f")
MARKER_PERIOD = 0.5
# How long without a marker before the board is presumed gone.
SILENCE = 1.5
# How often to look again while the board is not answering.
CHECK_EVERY = 2.0
# How often to look at main.py for a save.
POLL_FILES = 0.3

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
        print("Setting up the tools that talk to the board. Once only.", flush=True)
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
    and records when it was last seen. Protocol matching and the output the
    participant sees both work on the stripped stream.
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
            self.marker_id = match.group(1).decode()
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
        # A lone \x1e that never completed is real output. Let it through.
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
        self.exec_raw(f"f=open({name!r},'wb')".encode())
        for start in range(0, len(data), 512):
            self.exec_raw(b"f.write(" + repr(data[start:start + 512]).encode() + b")")
        self.exec_raw(b"f.close()")

    def soft_reset(self) -> None:
        """Leave raw REPL, then soft reset. MicroPython then runs main.py."""
        self.write(b"\x02")
        self.drain(0.15)
        self.write(b"\x04")
        # Boot takes about a second before the first marker. Do not call that
        # silence.
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

    "bare" means a board that answers the REPL but runs no boot.py of ours,
    for example a fresh simulation whose flash was wiped. None means nothing
    answered: the simulation is stopped, or paused because its tab is hidden.
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
# What the participant sees.


class Output:
    """Writes the board's bytes to the terminal, minus MicroPython's own noise.

    Complete lines go straight through. A partial line waits a moment in case
    it is the start of something to hide, then goes through as well.
    """

    NOISE = (b"MPY: soft reboot",)
    BANNER = b"MicroPython v"
    HELP = b'Type "help()" for more information.'

    def __init__(self):
        self.partial = b""
        self.partial_since = 0.0
        self.hide_help = False
        self.finished_shown = False

    def feed(self, data: bytes) -> None:
        if data:
            self.partial += data
            self.partial_since = time.monotonic()
        while b"\n" in self.partial:
            line, self.partial = self.partial.split(b"\n", 1)
            self._line(line + b"\n")
        if self.partial and time.monotonic() - self.partial_since > 0.15:
            if self.hide_help and self.partial.strip() == b">>>":
                self.partial = b""
                self.hide_help = False
                return
            if not self._could_be_noise(self.partial):
                self._emit(self.partial)
                self.partial = b""

    def _could_be_noise(self, data: bytes) -> bool:
        head = data.lstrip(b"\r")
        return any(n.startswith(head) for n in self.NOISE + (self.BANNER, self.HELP))

    def _line(self, line: bytes) -> None:
        text = line.strip()
        if text in self.NOISE:
            return
        if text.startswith(self.BANNER):
            self.hide_help = True
            self.finished_shown = True
            self._emit(f"\n--- {SCRIPT} finished. Save it to run it again. ---\n".encode())
            return
        if self.hide_help and text == self.HELP:
            return
        self._emit(line)

    def _emit(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()

    def flush(self) -> None:
        if self.partial and not self.hide_help:
            self._emit(self.partial)
        self.partial = b""


def say(text: str) -> None:
    print(text, flush=True)


# ---------------------------------------------------------------------------
# The watcher.

_rerun = threading.Event()
_repl_conn = None  # the task's socket while it holds the board


def file_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def sources_digest() -> str:
    return hashlib.sha1(file_bytes(os.path.join(ROOT, SCRIPT)) + b"\0" + file_bytes(BOOT_SOURCE)).hexdigest()


def run_code(board: Board, why: str) -> None:
    """Copy boot.py and main.py to the board and soft reset it."""
    board.enter_raw()
    board.write_file("boot.py", file_bytes(BOOT_SOURCE))
    board.write_file(SCRIPT, file_bytes(os.path.join(ROOT, SCRIPT)))
    say(f"\n--- {SCRIPT}, {why} ---")
    board.soft_reset()


def connect_and_run(why: str) -> Board:
    """Connect, copy, reset. Keeps trying while the board does not answer."""
    hinted = False
    while True:
        if not port_is_open():
            raise BoardGone("port closed")
        board = None
        try:
            board = Board()
            run_code(board, why)
            return board
        except (BoardGone, OSError) as error:
            if board is not None:
                board.close()
            if not hinted:
                say("\n  The board is not answering.")
                say("  Almost always this: the Wokwi tab is not the visible tab, or the")
                say("  simulation is stopped. Wokwi pauses a hidden tab. Click the Wokwi")
                say(f"  tab and check it is running. Trying again every {CHECK_EVERY:g} seconds.")
                say(f"  ({error})")
                hinted = True
            if _rerun.wait(CHECK_EVERY):
                _rerun.clear()


def stream(board: Board, output: Output) -> str:
    """Show output until something needs a new run. Returns why."""
    digest = sources_digest()
    next_poll = time.monotonic() + POLL_FILES
    quiet_said = False
    next_check = 0.0
    while True:
        if _repl_conn is not None:
            return "repl"
        if _rerun.is_set():
            _rerun.clear()
            return "run again"

        output.feed(board.read())

        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + POLL_FILES
            current = sources_digest()
            if current != digest:
                # Give the editor a moment to finish writing.
                time.sleep(0.2)
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
        if verdict == "bare" or verdict != board.marker_id:
            return "started in Wokwi" if quiet_said else "restarted in Wokwi"
        # Same id: the board never restarted, this connection just went quiet.
        return "reconnected"


def control_thread(lock: socket.socket) -> None:
    """Tasks connect to the lock port and send one line: send or repl."""
    global _repl_conn
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
        if line == b"repl":
            conn.settimeout(None)
            _repl_conn = conn
        else:
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


def hand_to_repl(board) -> None:
    """Give the board to the prompt task until it closes its socket."""
    global _repl_conn
    conn = _repl_conn
    say("\n  Handing the board to the MicroPython prompt. Close it to come back here.")
    if board is not None:
        board.close()
    with contextlib.suppress(OSError):
        conn.sendall(b"ok\n")
        while conn.recv(1024):
            pass
    conn.close()
    _repl_conn = None


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


def watch() -> None:
    lock = claim_single_instance()
    if lock is None:
        say(f"[pid {os.getpid()}] Another watcher has the board. Nothing to do here.")
        return
    say(f"[pid {os.getpid()}] Board output. Save {SCRIPT} (Cmd+S or Ctrl+S) and it runs on the board.")
    say("Everything it prints appears here. Wokwi's own terminal stays empty.")
    threading.Thread(target=control_thread, args=(lock,), daemon=True).start()
    if sys.stdin.isatty():
        threading.Thread(target=stdin_thread, daemon=True).start()

    output = Output()
    while True:
        if not port_is_open():
            say(f"\nWaiting for the simulator on port {PORT}. Press Start in the Wokwi tab.")
            while not port_is_open():
                time.sleep(0.5)
        why = "simulator started"
        board = None
        while True:
            if _repl_conn is not None:
                hand_to_repl(board)
                board = None
                why = "after the prompt"
            try:
                if board is None:
                    board = connect_and_run(why)
                why = stream(board, output)
            except BoardGone as error:
                if str(error) == "port closed":
                    break
                why = "reconnected"
            except OSError:
                why = "reconnected"
            output.flush()
            if why == "port closed":
                break
            if why == "reconnected":
                if board is not None:
                    board.close()
                board = None
                # Attach again without a reset. Nothing to replay.
                try:
                    board = Board()
                    say("\n  Reconnected.")
                    continue
                except Exception:
                    why = "reconnected"
                    board = None
                    continue
            if why == "repl":
                continue
            # saved, run again, restarted in Wokwi: copy and reset.
            if why in ("restarted in Wokwi", "started in Wokwi"):
                board.close()
                board = None
                continue
            try:
                run_code(board, why)
            except (BoardGone, OSError):
                board.close()
                board = None
        if board is not None:
            board.close()
        say("\nThe simulator is gone. Waiting for the next one.")


# ---------------------------------------------------------------------------
# The tasks.


def ask_watcher(line: bytes):
    """Send one line to a running watcher. Returns the socket, or None."""
    try:
        conn = socket.create_connection((HOST, LOCK_PORT), timeout=2)
        conn.sendall(line + b"\n")
        # The watcher pauses the marker before it answers. Give it time.
        conn.settimeout(15)
        conn.makefile("rb").readline()
        return conn
    except OSError:
        return None


def send_once() -> int:
    if not port_is_open():
        say("The simulator is not running. Press Start in the Wokwi tab first.")
        return 1
    conn = ask_watcher(b"send")
    if conn is not None:
        conn.close()
        say(f'Asked the watcher to run {SCRIPT} again. Look in "Board output".')
        return 0
    output = Output()
    try:
        board = connect_and_run("sent by hand")
        while True:
            output.feed(board.read())
    except KeyboardInterrupt:
        return 0


def prompt() -> int:
    """A MicroPython prompt in this terminal, keystroke by keystroke.

    The running program is interrupted first, because MicroPython only shows
    a prompt when nothing is running. Ctrl-] leaves. Markers are stripped on
    the way through like everywhere else.
    """
    try:
        import select
        import termios
        import tty
    except ImportError:
        say("The prompt needs a Unix terminal. In a Codespace it is one.")
        return 1
    board = Board()
    board.write(b"\r\x03\x02")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ready, _, _ = select.select([fd], [], [], 0.02)
            if ready:
                keys = os.read(fd, 1024)
                if b"\x1d" in keys:
                    return 0
                board.write(keys)
            data = board.read()
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        board.close()
        print()


def repl() -> int:
    if not port_is_open():
        say("The simulator is not running. Press Start in the Wokwi tab first.")
        return 1
    conn = ask_watcher(b"repl")
    say("Type Python at the board. Ctrl-] leaves, and your saved main.py runs again.")
    try:
        return prompt()
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    ensure_dependencies()
    mode = sys.argv[1] if len(sys.argv) > 1 else "--watch"
    try:
        if mode == "--once":
            sys.exit(send_once())
        elif mode == "--repl":
            sys.exit(repl())
        else:
            watch()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
