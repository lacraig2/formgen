"""Driving Word over COM, safely, and never on the critical path.

Word is present on the target machine and it is the only thing that can answer
certain questions authoritatively -- does this document trigger a repair
prompt, what does Word think this style resolves to, how many pages is it. So
we use it. But it is also a GUI application with modal dialogs, and a modal
dialog blocks a COM call **for ever**: there is no per-call timeout in the COM
plumbing. Everything here exists to contain that.

The rules, each of which is a real failure someone has shipped:

* **Start a dedicated hidden instance.** Attaching to the user's running Word
  means fighting their open documents and unsaved state, and quitting takes
  their session down with it.
* **Disable alerts, macros and autosave.** `AutomationSecurity` is the
  important one: a document with an AutoOpen macro will otherwise hang the
  process behind a security prompt nobody can see.
* **Quit in a `finally`, then verify.** An orphaned WINWORD.EXE holds a file
  lock on everything it had open and silently breaks the next run, so the
  session records which processes it started and kills any that outlive it.
* **Bound every call in wall-clock time.** The work runs on a worker thread;
  if it overruns, the caller gets a timeout error and the instance is killed,
  rather than a build that hangs until someone notices next morning.
* **Never call COM without an interactive desktop.** From a service or a
  session with no window station, Word fails in confusing ways. Detect and
  skip instead.

Every caller must degrade gracefully: `available()` is false on Linux, in CI,
and on a Windows box without pywin32, and nothing in the core may require this
module to work.
"""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable

# Word enum values we use, spelled out so nothing depends on the constants
# module being generated (win32com's late binding does not provide it).
WD_ALERTS_NONE = 0
MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3
WD_FORMAT_PDF = 17
WD_FORMAT_XML_DOCUMENT = 12
WD_DO_NOT_SAVE_CHANGES = 0

DEFAULT_TIMEOUT = 120.0


class WordUnavailable(RuntimeError):
    """Word cannot be driven here. Always a skip, never a failure."""


class WordTimeout(RuntimeError):
    """A COM call overran its budget -- almost always an invisible dialog."""


def on_windows() -> bool:
    return sys.platform == "win32"


def has_interactive_desktop() -> bool:
    """Word needs a window station. Services and some CI agents have none."""
    if not on_windows():
        return False
    try:
        import win32process  # noqa: F401
        import win32service

        station = win32service.OpenWindowStation(
            "WinSta0", False, win32service.WINSTA_ENUMDESKTOPS
        )
        return station is not None
    except Exception:
        # Not being able to prove it is not the same as proving it is not.
        # Assume yes on a desktop Windows and let the open attempt decide.
        return platform.win32_ver()[0] != ""


def available() -> tuple[bool, str]:
    """(usable, reason). The reason is shown to the user when we skip."""
    if not on_windows():
        return False, f"Word automation needs Windows; this is {sys.platform}."
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False, (
            "pywin32 is not importable. It ships with Anaconda on Windows; "
            "in a bare environment, `pip install --user pywin32`."
        )
    if not has_interactive_desktop():
        return False, (
            "no interactive desktop session; Word cannot be automated from a "
            "service or a headless agent."
        )
    return True, ""


def winword_pids() -> set[int]:
    """Currently running WINWORD.EXE process ids."""
    if not on_windows():
        return set()
    try:
        output = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in output.splitlines():
        fields = [f.strip('" ') for f in line.split('","')]
        if len(fields) > 1 and fields[1].isdigit():
            pids.add(int(fields[1]))
    return pids


def _kill(pid: int) -> None:
    if not on_windows():
        return
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


@dataclass
class WordSession:
    """A dedicated, hidden Word instance that always cleans up after itself."""

    visible: bool = False
    app: Any = None
    _before: set[int] = None  # type: ignore[assignment]

    def __enter__(self) -> Any:
        usable, reason = available()
        if not usable:
            raise WordUnavailable(reason)
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        self._before = winword_pids()
        # DispatchEx, not Dispatch: Dispatch attaches to the user's running
        # Word if there is one, and quitting it would close their documents.
        self.app = win32com.client.DispatchEx("Word.Application")
        self.app.Visible = self.visible
        self.app.DisplayAlerts = WD_ALERTS_NONE
        try:
            self.app.AutomationSecurity = MSO_AUTOMATION_SECURITY_FORCE_DISABLE
            self.app.Options.SaveInterval = 0
            self.app.ScreenUpdating = False
        except Exception:
            # Older or policy-restricted builds refuse some of these. None is
            # required for correctness; they only reduce the ways Word can
            # stop and wait for a human.
            pass
        return self.app

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.app is not None:
                self.app.Quit(WD_DO_NOT_SAVE_CHANGES)
        except Exception:
            pass
        self.app = None
        try:
            import pythoncom

            pythoncom.CoUninitialize()
        except Exception:
            pass
        # An orphan holds file locks and silently breaks the next run, so
        # "we asked it to quit" is not good enough.
        for pid in winword_pids() - (self._before or set()):
            _kill(pid)


def run_guarded(
    work: Callable[[Any], Any],
    timeout: float = DEFAULT_TIMEOUT,
    visible: bool = False,
) -> Any:
    """Run `work(app)` against a fresh Word, bounded in wall-clock time.

    The worker thread is a daemon: if Word wedges behind a dialog the thread
    cannot be interrupted, so we abandon it, kill the process it was talking
    to, and return control. Leaking a thread is survivable; hanging the
    process until a human notices is not.
    """
    results: queue.Queue = queue.Queue(maxsize=1)
    before = winword_pids()

    def target() -> None:
        try:
            with WordSession(visible=visible) as app:
                results.put(("ok", work(app)))
        except BaseException as exc:  # noqa: BLE001 - relayed to the caller
            results.put(("error", exc))

    thread = threading.Thread(target=target, name="formgen-word", daemon=True)
    thread.start()
    try:
        status, payload = results.get(timeout=timeout)
    except queue.Empty:
        for pid in winword_pids() - before:
            _kill(pid)
        raise WordTimeout(
            f"Word did not respond within {timeout:.0f}s -- it is most likely "
            "showing a dialog we cannot see. The instance has been terminated."
        ) from None
    if status == "error":
        raise payload
    return payload


def open_document(app: Any, path: str | os.PathLike) -> Any:
    """Open read-only, converting nothing and asking nothing."""
    return app.Documents.Open(
        FileName=str(os.path.abspath(path)),
        ConfirmConversions=False,
        ReadOnly=True,
        AddToRecentFiles=False,
        Revert=True,
        Visible=False,
    )
