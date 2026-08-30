#!/usr/bin/env python3
"""
RecoverAI task runner -- one command surface for every platform.

Why this file exists instead of a Makefile:
    `make` is not installed on a stock Windows machine, and the shell one-liners
    that make files are full of (`source .venv/bin/activate && ...`) simply do
    not run in cmd.exe or PowerShell. Rather than maintain a Makefile plus a
    parallel set of .bat files that inevitably drift apart, every task lives
    here once, in Python -- which is already a hard prerequisite of the project.

Usage (identical on Windows, macOS and Linux):

    python dev.py doctor      Check the environment and report what is missing
    python dev.py setup       Create the venv, install everything, write .env files
    python dev.py seed        Reset and populate the demo database
    python dev.py train       Train the recovery-propensity model
    python dev.py backend     Run the FastAPI server        (http://127.0.0.1:8000)
    python dev.py frontend    Run the Next.js dev server    (http://localhost:3000)
    python dev.py test        Run the backend test suite
    python dev.py demo        setup + seed + train, then print the demo checklist
    python dev.py start       Run backend and frontend together in one terminal

Design notes:
    * No third-party imports. This script must run on a bare interpreter, before
      anything is installed -- it is the thing that does the installing.
    * Every path is built with pathlib, so it is correct on both `C:\\Users\\...`
      and `/Users/...`.
    * Every subprocess is invoked with an argument LIST, never a shell string,
      so paths containing spaces (`C:\\Users\\Reshu Kumari\\...`) do not break.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV = ROOT / ".venv"

IS_WINDOWS = os.name == "nt"

#: Location of the interpreter and scripts inside a venv differs by platform:
#: Windows uses `Scripts\`, everything else uses `bin/`. Getting this wrong is
#: the single most common cross-platform venv bug.
VENV_BIN = VENV / ("Scripts" if IS_WINDOWS else "bin")
VENV_PY = VENV_BIN / ("python.exe" if IS_WINDOWS else "python")

MIN_PY = (3, 10)
MAX_TESTED_PY = (3, 13)


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------
# ASCII only. The default Windows console code page (cp1252) raises
# UnicodeEncodeError on box-drawing characters and most emoji, which turns a
# cosmetic banner into a crash on the exact machine we care most about.

def say(msg: str) -> None:
    print(msg, flush=True)


def head(msg: str) -> None:
    say("")
    say("=" * 68)
    say(f"  {msg}")
    say("=" * 68)


def ok(msg: str) -> None:
    say(f"  [ OK ] {msg}")


def warn(msg: str) -> None:
    say(f"  [WARN] {msg}")


def fail(msg: str) -> None:
    say(f"  [FAIL] {msg}")


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[valid-type]
    fail(msg)
    sys.exit(code)


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> int:
    """
    Run a command, streaming its output.

    ``cmd`` is always a list. Passing a single string would require ``shell=True``,
    which re-introduces quoting bugs on any path containing a space -- and on
    Windows a user's home directory very often does.
    """
    printable = " ".join(str(c) for c in cmd)
    say(f"  $ {printable}")
    merged = {**os.environ, **(env or {})}
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=merged)
    if check and result.returncode != 0:
        die(f"Command failed with exit code {result.returncode}: {printable}")
    return result.returncode


def which(name: str) -> str | None:
    """
    Locate an executable.

    ``shutil.which`` is used rather than a hardcoded name because on Windows npm
    is installed as ``npm.cmd``; looking for a bare ``npm`` finds nothing.
    """
    return shutil.which(name)


def venv_python() -> Path:
    """Return the venv interpreter, or explain how to create it."""
    if not VENV_PY.exists():
        die(
            "The virtual environment is missing.\n"
            "         Run this first:  python dev.py setup"
        )
    return VENV_PY


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(_args: argparse.Namespace) -> None:
    """Report what is present, what is missing, and exactly how to fix it."""
    head("RecoverAI environment check")

    say(f"  Platform      : {platform.system()} {platform.release()} ({platform.machine()})")
    say(f"  Python        : {sys.version.split()[0]}  ({sys.executable})")
    say(f"  Project root  : {ROOT}")
    say("")

    problems = 0

    v = sys.version_info
    if (v.major, v.minor) < MIN_PY:
        fail(f"Python {v.major}.{v.minor} is too old. Install Python 3.10 or newer "
             "from https://www.python.org/downloads/")
        problems += 1
    elif (v.major, v.minor) > MAX_TESTED_PY:
        warn(f"Python {v.major}.{v.minor} is newer than the tested range "
             f"({MIN_PY[0]}.{MIN_PY[1]}-{MAX_TESTED_PY[0]}.{MAX_TESTED_PY[1]}). "
             "If scikit-learn or numpy fail to install, use Python 3.13.")
    else:
        ok(f"Python {v.major}.{v.minor} is within the tested range")

    node = which("node")
    if node:
        try:
            out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20)
            ok(f"Node.js {out.stdout.strip()} at {node}")
        except Exception:
            ok(f"Node.js found at {node}")
    else:
        fail("Node.js not found. The frontend needs it. Install the LTS build from "
             "https://nodejs.org/ and reopen your terminal.")
        problems += 1

    if which("npm"):
        ok("npm is on PATH")
    else:
        fail("npm not found (it ships with Node.js). Reopen your terminal after installing Node.")
        problems += 1

    if VENV_PY.exists():
        ok(f"Virtual environment ready at {VENV}")
    else:
        warn("No virtual environment yet -- run: python dev.py setup")

    if (FRONTEND / "node_modules").exists():
        ok("Frontend dependencies installed")
    else:
        warn("Frontend dependencies not installed -- run: python dev.py setup")

    env_file = BACKEND / ".env"
    if env_file.exists():
        ok("backend/.env exists")
    else:
        warn("backend/.env missing -- `python dev.py setup` creates it from the example. "
             "The app runs without credentials, so this is not fatal.")

    db = BACKEND / "data" / "recoverai.db"
    if db.exists():
        ok(f"Demo database present ({db.stat().st_size // 1024} KB)")
    else:
        warn("No database yet -- run: python dev.py seed")

    model = BACKEND / "models" / "propensity_model.joblib"
    if model.exists():
        ok("Trained propensity model present")
    else:
        warn("Model not trained -- run: python dev.py train  "
             "(the app falls back to a heuristic until then)")

    say("")
    if problems:
        fail(f"{problems} blocking problem(s) found. Fix those first.")
        sys.exit(1)
    ok("Environment looks good.")


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def _copy_env_templates() -> None:
    """Create .env files from the checked-in examples if they are absent."""
    pairs = [
        (BACKEND / ".env.example", BACKEND / ".env"),
        (FRONTEND / ".env.local.example", FRONTEND / ".env.local"),
    ]
    for src, dst in pairs:
        if dst.exists():
            ok(f"{dst.relative_to(ROOT)} already exists (left untouched)")
        elif src.exists():
            # Never overwrite: the file may hold the reviewer's own API keys.
            shutil.copyfile(src, dst)
            ok(f"Created {dst.relative_to(ROOT)} from the example")
        else:
            warn(f"Missing template {src.relative_to(ROOT)}")


def cmd_setup(args: argparse.Namespace) -> None:
    """Create the venv, install Python and Node dependencies, write .env files."""
    head("Setting up RecoverAI")

    v = sys.version_info
    if (v.major, v.minor) < MIN_PY:
        die(f"Python {v.major}.{v.minor} is too old; 3.10+ required.")

    if not VENV_PY.exists():
        say(f"  Creating virtual environment at {VENV}")
        run([sys.executable, "-m", "venv", str(VENV)])
        ok("Virtual environment created")
    else:
        ok("Virtual environment already present")

    py = str(VENV_PY)
    # Upgrading pip first matters: older pip versions do not understand newer
    # wheel tags and will try to build numpy from source on Windows, which fails
    # without Visual C++ Build Tools installed.
    run([py, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    run([py, "-m", "pip", "install", "-r", str(BACKEND / "requirements.txt")])
    ok("Python dependencies installed")

    _copy_env_templates()

    if args.backend_only:
        warn("Skipping frontend install (--backend-only)")
    else:
        npm = which("npm")
        if not npm:
            warn("npm not found -- skipping the frontend install. "
                 "Install Node.js LTS from https://nodejs.org/ then re-run setup.")
        else:
            say("  Installing frontend dependencies (this takes a minute or two)")
            run([npm, "install"], cwd=FRONTEND)
            ok("Frontend dependencies installed")

    head("Setup complete")
    say("  Next:")
    say("    python dev.py seed      # create the demo data")
    say("    python dev.py train     # train the ML model")
    say("    python dev.py start     # run both servers")


# ---------------------------------------------------------------------------
# seed / train / test
# ---------------------------------------------------------------------------

def cmd_seed(_args: argparse.Namespace) -> None:
    """Rebuild the demo database from scratch."""
    head("Seeding the demo database")
    run([str(venv_python()), "-m", "app.db.seed"], cwd=BACKEND)


def cmd_train(_args: argparse.Namespace) -> None:
    """Train and save the recovery-propensity model."""
    head("Training the recovery-propensity model")
    run([str(venv_python()), "-m", "app.ml.train"], cwd=BACKEND)


def cmd_test(args: argparse.Namespace) -> None:
    """Run the backend test suite."""
    head("Running the test suite")
    cmd = [str(venv_python()), "-m", "pytest"]
    if args.verbose:
        cmd.append("-v")
    if args.k:
        cmd += ["-k", args.k]
    run(cmd, cwd=BACKEND)


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------

def _backend_cmd(reload: bool = True) -> list[str]:
    cmd = [str(venv_python()), "-m", "uvicorn", "app.main:app",
           "--host", "127.0.0.1", "--port", "8000"]
    if reload:
        cmd.append("--reload")
    return cmd


def cmd_backend(args: argparse.Namespace) -> None:
    head("Starting the RecoverAI API on http://127.0.0.1:8000")
    say("  Interactive API docs: http://127.0.0.1:8000/docs")
    say("  Stop with Ctrl+C")
    run(_backend_cmd(reload=not args.no_reload), cwd=BACKEND, check=False)


def cmd_frontend(_args: argparse.Namespace) -> None:
    npm = which("npm")
    if not npm:
        die("npm not found. Install Node.js LTS from https://nodejs.org/ and reopen your terminal.")
    if not (FRONTEND / "node_modules").exists():
        die("Frontend dependencies are not installed. Run: python dev.py setup")
    head("Starting the RecoverAI console on http://localhost:3000")
    say("  Stop with Ctrl+C")
    run([npm, "run", "dev"], cwd=FRONTEND, check=False)


def _spawn(cmd: list[str], cwd: Path) -> "subprocess.Popen[bytes]":
    """
    Start a child that can be shut down as a whole tree later.

    On POSIX the child gets its own session (``start_new_session=True``) so that
    ``os.killpg`` can take down it and everything it spawned in one call.
    Windows has no session concept here; ``_stop_tree`` uses ``taskkill /T``
    instead, so nothing extra is needed at spawn time.
    """
    kwargs: dict[str, object] = {"cwd": str(cwd)}
    if not IS_WINDOWS:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]


def _stop_tree(proc: "subprocess.Popen[bytes]") -> None:
    """
    Stop a child process and every process it started.

    Killing only the direct child is not enough. `npm run dev` is a wrapper
    around node, and `uvicorn --reload` is a supervisor around the process that
    actually holds the socket, so terminating the visible child leaves an
    invisible one on the port -- and the next start fails against a server the
    user has no obvious way to find or kill.

    Both branches are best-effort: this runs in a ``finally`` during shutdown,
    and a failure to reap a child that has already exited must not become the
    last thing the user sees.
    """
    if proc.poll() is not None:
        return

    if IS_WINDOWS:
        # /T includes the whole tree, /F forces it. Output is discarded because
        # "process not found" is a normal race here, not an error worth showing.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()

    # Give the tree a moment to exit cleanly before forcing what is left.
    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()


def cmd_start(_args: argparse.Namespace) -> None:
    """
    Run both servers from a single terminal.

    Two child processes are managed rather than requiring two terminal windows,
    because "open a second terminal, activate the venv again, cd again" is where
    a reviewer following a README most often gives up.
    """
    npm = which("npm")
    if not npm:
        die("npm not found. Install Node.js LTS from https://nodejs.org/ and reopen your terminal.")
    if not (FRONTEND / "node_modules").exists():
        die("Frontend dependencies are not installed. Run: python dev.py setup")

    head("Starting RecoverAI (backend + frontend)")
    say("  API      : http://127.0.0.1:8000        (docs at /docs)")
    say("  Console  : http://localhost:3000")
    say("  Stop both with Ctrl+C")
    say("")

    # Neither child gets its own Windows process group. `CREATE_NEW_PROCESS_GROUP`
    # looks like the tidy choice, but it stops the console's Ctrl+C from reaching
    # uvicorn -- whose Windows shutdown path depends on receiving it -- and it
    # does not help with the real problem, which is that neither child is a
    # single process:
    #
    #   backend  -> uvicorn's reloader, which spawns a separate process that is
    #               the one actually holding port 8000
    #   frontend -> npm.cmd -> cmd.exe -> node, and node is the one on port 3000
    #
    # Terminating the direct child therefore leaves a grandchild still bound to
    # the port, and the next `dev.py start` fails with "address already in use"
    # against a server the user cannot see. `_stop_tree` below kills the whole
    # tree on both platforms.
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(_spawn(_backend_cmd(reload=True), BACKEND))
        # Small stagger so the API is listening before the first page load; it
        # only removes a confusing "cannot reach the API" flash on first render.
        time.sleep(2.0)
        procs.append(_spawn([npm, "run", "dev"], FRONTEND))

        while True:
            for p in procs:
                code = p.poll()
                if code is not None:
                    warn(f"A server exited with code {code}. Shutting the other one down.")
                    raise KeyboardInterrupt
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("")
        say("  Shutting down...")
    finally:
        for p in procs:
            _stop_tree(p)
        ok("Stopped.")


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------

def cmd_demo(args: argparse.Namespace) -> None:
    """One command that takes a fresh clone to a demo-ready state."""
    cmd_setup(args)
    cmd_seed(args)
    cmd_train(args)
    head("RecoverAI is ready to demo")
    say("  Start everything:")
    say("      python dev.py start")
    say("")
    say("  Then open http://localhost:3000 and follow docs/07-DEMO-SCRIPT.md")
    say("")
    say("  With no API keys configured you will see two honest badges in the UI:")
    say("      'Rule-based'  -- deterministic planner instead of Gemini")
    say("      'Simulated'   -- in-process gateway instead of Razorpay Test Mode")
    say("  Add keys to backend/.env to switch either one on.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python dev.py",
        description="RecoverAI task runner (works identically on Windows, macOS and Linux)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("doctor", help="Check the environment and report what is missing")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="Create the venv and install all dependencies")
    s.add_argument("--backend-only", action="store_true", help="Skip the npm install")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("seed", help="Reset and populate the demo database")
    s.set_defaults(func=cmd_seed)

    s = sub.add_parser("train", help="Train the recovery-propensity model")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("test", help="Run the backend test suite")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("-k", help="Only run tests matching this expression")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("backend", help="Run the FastAPI server")
    s.add_argument("--no-reload", action="store_true")
    s.set_defaults(func=cmd_backend)

    s = sub.add_parser("frontend", help="Run the Next.js dev server")
    s.set_defaults(func=cmd_frontend)

    s = sub.add_parser("start", help="Run backend and frontend together")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("demo", help="setup + seed + train, then print the demo checklist")
    s.add_argument("--backend-only", action="store_true")
    s.set_defaults(func=cmd_demo)

    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        say("")
        say("  Interrupted.")
        sys.exit(130)


if __name__ == "__main__":
    main()
