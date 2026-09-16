# cyberdeck

Your microcontroller project. A Raspberry Pi Pico, simulated, running MicroPython.

Nothing here needs a physical board. The Pico, the LED and the button are all
simulated by Wokwi inside your editor.

## First run

1. Press <kbd>F1</kbd> and run **Wokwi: Start Simulator**. The board appears in a tab.
2. Press <kbd>F1</kbd>, run **Tasks: Run Task**, choose **Run on Wokwi**.
3. Press the green button in the simulator. The LED toggles.

Keep the Wokwi tab visible while the task runs. Wokwi pauses the simulation
when its tab is hidden, and the task then fails with "could not enter raw repl".

## The one thing you have to do yourself

Wokwi needs a licence, free for personal use. Press <kbd>F1</kbd> and run
**Wokwi: Request a New License**. Your browser confirms it and the licence
lands back in the editor. Once only.

## What is where

| File | What it does |
|---|---|
| `main.py` | Your code. Start here. |
| `diagram.json` | The circuit. Add parts and wire them up. |
| `wokwi.toml` | Points Wokwi at the MicroPython firmware. |
| `RPI_PICO-*.uf2` | MicroPython itself, running on the simulated board. |
| `.vscode/tasks.json` | The "Run on Wokwi" and REPL tasks. |

## Things worth knowing

**The board forgets everything when the simulator restarts.** Its filesystem
is not saved. That is why "Run on Wokwi" copies `main.py` across every time
rather than assuming it is already there.

**Your editor knows MicroPython, not desktop Python.** `machine`, `rp2` and
the rest come from type stubs installed with `requirements.txt`. If imports go
red, run `pip install --user -r requirements.txt` and reload the window.

**A prompt straight to the board** is the other task, **Open a MicroPython
prompt**. Type Python at the running board and watch it respond. Ctrl-] leaves.

## Time tracking

Hackatime records the hours you spend here, which is what your project is
reviewed on. It is already set up. Nothing to paste, nothing to install.

If the editor asks you for a Hackatime API key, something went wrong. Say so
in the program channel rather than pasting one in.

## Shipping

A finished project needs a public repository with a real commit history, a
README explaining what you built, and a way for someone else to see it work.
For hardware that is usually a short video or a Wokwi link. Commit as you go:
one commit at the end is not a history.
