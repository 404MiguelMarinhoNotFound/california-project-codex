"""
Driving Phone Link: dial, notice the call, hang up, and route the microphone.

Everything here was found on the real laptop on 2026-10-06, and most of it is
counter-intuitive:

- A `tel:` link only PRE-FILLS Phone Link's dial pad. It never dials.
- A synthetic mouse click on the call button, and UI Automation's Invoke on it,
  are both ignored. What dials is: give the button keyboard focus through UI
  Automation (`SetFocus` on AutomationId `ButtonCall`), then a real Enter key.
- The dial pad can still hold the LAST number. The button is enabled either
  way, so before pressing it we wait until the pad shows the number we asked
  for -- otherwise a stale number gets called.
- Phone Link records from the Windows DEFAULT microphone (console role), not
  the "communications" one. Her voice reaches the call only if the default mic
  is the VB-CABLE output for the length of the call. `MicRoute` swaps it in and
  restores whatever was there before, in a `finally`.

All of it goes through PowerShell and the .NET UI Automation client, so there
is no new Python dependency and nothing here imports anything Windows-only at
module scope. Every PowerShell call goes through `_run_ps`, the one boundary
the tests patch.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable

logger = logging.getLogger(__name__)

_PS_TIMEOUT_S = 25
# Consecutive "End button gone" readings before a call counts as hung up.
_GONE_READINGS = 2

# Default audio endpoint get/set (IMMDeviceEnumerator / IPolicyConfig). The
# vtable order is what matters; unused slots are placeholders.
_AUDIO_TYPES = r"""
using System; using System.Runtime.InteropServices;
[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumeratorCal { int EnumAudioEndpoints(); [PreserveSig] int GetDefaultAudioEndpoint(int flow, int role, out IMMDeviceCal dev); }
[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceCal { int Activate(); int OpenPropertyStore(); [PreserveSig] int GetId([MarshalAs(UnmanagedType.LPWStr)] out string id); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCal {}
[ComImport, Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IPolicyConfigCal { void a(); void b(); void c(); void d(); void e(); void f(); void g(); void h(); void i(); void j();
  [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string id, int role); }
[ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")] class PolicyConfigClientCal {}
public static class CalAudio {
  public static string GetDefault(int flow) {
    var e = (IMMDeviceEnumeratorCal)new MMDeviceEnumeratorCal(); IMMDeviceCal d; string id;
    Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(flow, 0, out d)); Marshal.ThrowExceptionForHR(d.GetId(out id)); return id; }
  public static string GetDefaultCapture() { return GetDefault(1); }
  public static void SetDefault(string id) {
    var p = (IPolicyConfigCal)new PolicyConfigClientCal();
    for (int r = 0; r < 3; r++) Marshal.ThrowExceptionForHR(p.SetDefaultEndpoint(id, r)); }
}
"""

_GET_MIC = "Add-Type -TypeDefinition @'\n" + _AUDIO_TYPES + "\n'@\n[CalAudio]::GetDefaultCapture()"

_GET_SPEAKER = "Add-Type -TypeDefinition @'\n" + _AUDIO_TYPES + "\n'@\n[CalAudio]::GetDefault(0)"

_SET_MIC = "Add-Type -TypeDefinition @'\n" + _AUDIO_TYPES + "\n'@\n[CalAudio]::SetDefault('__DEVICE__'); 'OK'"

_FIND_ENDPOINT = (
    "$d = Get-PnpDevice -Class AudioEndpoint | Where-Object { $_.Status -eq 'OK' -and "
    "$_.FriendlyName -eq '__NAME__' } | Select-Object -First 1; "
    "if ($d) { $d.InstanceId -replace '^SWD\\\\MMDEVAPI\\\\','' }"
)

# Pressing Enter is only safe once Phone Link is the FOREGROUND window and the
# button really has keyboard focus. UIA SetFocus alone does not bring a window
# forward when asked from a background process (Windows' foreground lock): on
# 2026-10-07 the foreground window after a dial was the Claude app, so the
# Enter could have landed anywhere. So: lift the lock (attach to the current
# foreground thread, plus the standard ALT tap), bring the window forward,
# then VERIFY both the foreground window and the focused element before any
# key is sent. If either check fails, no key is sent at all.
_FOREGROUND_PS = r"""
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class CalFg {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr p);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
  public static bool Bring(IntPtr h) {
    if (GetForegroundWindow() == h) return true;
    if (IsIconic(h)) ShowWindow(h, 9);
    uint fg = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero);
    uint me = GetCurrentThreadId();
    AttachThreadInput(me, fg, true);
    keybd_event(0x12, 0, 0, UIntPtr.Zero); keybd_event(0x12, 0, 2, UIntPtr.Zero);
    SetForegroundWindow(h);
    AttachThreadInput(me, fg, false);
    return GetForegroundWindow() == h;
  }
}
'@
function PressEnterOn($win, $el, $id) {
  $h = [IntPtr]$win.Current.NativeWindowHandle
  if (-not [CalFg]::Bring($h)) { Start-Sleep -Milliseconds 300; if (-not [CalFg]::Bring($h)) { return 'NOT_FOREGROUND' } }
  $el.SetFocus(); Start-Sleep -Milliseconds 300
  if ([CalFg]::GetForegroundWindow() -ne $h) { return 'NOT_FOREGROUND' }
  $f = $A::FocusedElement
  if (-not $f -or ($id -and $f.Current.AutomationId -ne $id)) { return 'NOT_FOCUSED' }
  [System.Windows.Forms.SendKeys]::SendWait('{ENTER}')
  return 'SENT'
}
"""

_UIA = (
    "Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms\n"
    "$A = [System.Windows.Automation.AutomationElement]\n"
    "$T = [System.Windows.Automation.TreeScope]\n"
    "function PhoneWindows { $all = $A::RootElement.FindAll($T::Children, "
    "[System.Windows.Automation.Condition]::TrueCondition); foreach ($w in $all) { "
    "try { $p = (Get-Process -Id $w.Current.ProcessId -ErrorAction Stop).ProcessName } catch { $p = '' }; "
    "if ($p -eq 'PhoneExperienceHost') { $w } } }\n"
    "function MainWindow { PhoneWindows | Where-Object { $_.Current.Name -eq 'Phone Link' } | Select-Object -First 1 }\n"
    # A call is recognised by its hang-up button, wherever Phone Link draws it.
    # Keying on a second window's title missed a real call on 2026-10-07: the
    # call went out, the dial step reported NO_CALL_WINDOW, and the session was
    # closed under it, so the callee heard silence.
    "function EndButton { foreach ($w in (PhoneWindows)) { $bs = $w.FindAll($T::Descendants, "
    "(New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, "
    "[System.Windows.Automation.ControlType]::Button))); foreach ($b in $bs) { "
    "if ($b.Current.Name -match '^(End|Hang up|Terminar|Desligar)') { return @($w, $b) } } }; return $null }\n"
    "function CallWindow { $e = EndButton; if ($e) { $e[0] } }\n"
    "function DumpPhone { foreach ($w in (PhoneWindows)) { 'WIN name=' + $w.Current.Name + ' cls=' + $w.Current.ClassName; "
    "$bs = $w.FindAll($T::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, "
    "[System.Windows.Automation.ControlType]::Button))); foreach ($b in $bs) { '  BTN ' + $b.Current.Name + ' id=' + $b.Current.AutomationId } } }\n"
    "function ById($root, $id) { $root.FindFirst($T::Descendants, "
    "(New-Object System.Windows.Automation.PropertyCondition($A::AutomationIdProperty, $id))) }\n"
    "function Digits($s) { ($s -replace '\\D', '') }\n"
)

# Only the scripts that press a key compile the foreground helper. The in-call
# check runs every couple of seconds for the whole call, so it must not pay an
# Add-Type C# compile each time while the audio threads need the CPU.
_UIA_KEYS = _UIA + _FOREGROUND_PS

# Prefill, wait for the pad to show THIS number, focus Call, press Enter, then
# wait for a call window to appear. Prints one status word on the last line.
_DIAL = _UIA_KEYS + r"""
$number = '__NUMBER__'; $want = (Digits $number); $tail = $want.Substring([Math]::Max(0, $want.Length - 9))
Start-Process ("tel:" + $number)
$deadline = (Get-Date).AddSeconds(10); $btn = $null; $ready = $false
do {
  Start-Sleep -Milliseconds 300
  $win = MainWindow
  if ($win -and (ById $win 'DialerPaneErrorTitle')) { 'PHONE_NOT_CONNECTED'; exit 0 }
  if ($win) {
    $btn = ById $win 'ButtonCall'
    $texts = $win.FindAll($T::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::Text)))
    foreach ($t in $texts) { if ((Digits $t.Current.Name).EndsWith($tail) -and (Digits $t.Current.Name).Length -ge 9) { $ready = $true } }
  }
} until (($ready -and $btn -and $btn.Current.IsEnabled) -or (Get-Date) -gt $deadline)
if (-not $ready) { 'NOT_PREFILLED'; exit 0 }
if (-not ($btn -and $btn.Current.IsEnabled)) { 'NO_BUTTON'; exit 0 }
$pressed = PressEnterOn $win $btn 'ButtonCall'
if ($pressed -ne 'SENT') { $pressed; exit 0 }
$deadline = (Get-Date).AddSeconds(4)
do { Start-Sleep -Milliseconds 300; if (EndButton) { 'DIALED'; exit 0 } } until ((Get-Date) -gt $deadline)
DumpPhone
'DIALED_UNCONFIRMED'
"""

_IN_CALL = _UIA + "if (CallWindow) { 'YES' } else { 'NO' }"

# Same check, but on a miss after the call was seen it logs every Phone Link
# button first: on 2026-10-08 the End button vanished 20s into a live call,
# and the next time it happens the log says what it turned into.
_IN_CALL_DUMP = _UIA + "if (CallWindow) { 'YES' } else { DumpPhone; 'NO' }"

_HANG_UP = _UIA_KEYS + r"""
$e = EndButton
if (-not $e) { 'NO_CALL'; exit 0 }
$pressed = PressEnterOn $e[0] $e[1] ''
if ($pressed -ne 'SENT') { $pressed; exit 0 }
Start-Sleep -Milliseconds 800
if (EndButton) { 'STILL_IN_CALL' } else { 'ENDED' }
"""

# --- Find my phone ----------------------------------------------------------
# The left pane's "Play sound" button (AutomationId RingMyPhoneIndicatorToggleButton,
# read off the live window 2026-10-08) rings the phone for ~20 seconds at full
# volume, even on silent. It is a toggle: ToggleState On while it rings. Like
# the Call button it ignores a synthetic click, so it takes the same
# focus-and-Enter, and the toggle state is what proves the press landed. If
# Phone Link is closed it is launched first (its package family name is fixed).
_PLAY_SOUND = _UIA_KEYS + r"""
function Ringing($b) { try { return ($b.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern).Current.ToggleState -eq 'On') } catch { return $false } }
$win = MainWindow
if (-not $win) {
  Start-Process 'shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App'
  $deadline = (Get-Date).AddSeconds(15)
  do { Start-Sleep -Milliseconds 500; $win = MainWindow } until ($win -or (Get-Date) -gt $deadline)
}
if (-not $win) { 'NO_WINDOW'; exit 0 }
$deadline = (Get-Date).AddSeconds(8); $btn = $null
do { $btn = ById $win 'RingMyPhoneIndicatorToggleButton'; if (-not $btn) { Start-Sleep -Milliseconds 400 } } until ($btn -or (Get-Date) -gt $deadline)
if (-not $btn) { 'NO_BUTTON'; exit 0 }
if (Ringing $btn) { 'ALREADY_PLAYING'; exit 0 }
if (-not $btn.Current.IsEnabled) { 'DISABLED'; exit 0 }
# The toggle only opens a confirmation flyout ("The sound will play for 20
# seconds...") whose own "Play sound" button, a plain button with no
# AutomationId, does the ringing. Found live 2026-10-08: pressing the toggle
# alone leaves the flyout up and the phone silent. A flyout already open from
# an earlier try is reused rather than toggled shut.
function Flyout($w) {
  foreach ($b in $w.FindAll($T::Descendants, (New-Object System.Windows.Automation.PropertyCondition($A::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))) {
    if ($b.Current.Name -match '^(Play sound|Reproduzir som)$' -and $b.Current.AutomationId -eq '') { return $b }
  }
  return $null
}
$confirm = Flyout $win
if (-not $confirm) {
  $pressed = PressEnterOn $win $btn 'RingMyPhoneIndicatorToggleButton'
  if ($pressed -ne 'SENT') { $pressed; exit 0 }
  $deadline = (Get-Date).AddSeconds(4)
  do { Start-Sleep -Milliseconds 250; $confirm = Flyout $win } until ($confirm -or (Get-Date) -gt $deadline)
}
if (-not $confirm) { 'NO_CONFIRM'; exit 0 }
$pressed = PressEnterOn $win $confirm ''
if ($pressed -ne 'SENT') { $pressed; exit 0 }
$deadline = (Get-Date).AddSeconds(5)
do { Start-Sleep -Milliseconds 300; if (Ringing $btn) { 'PLAYING'; exit 0 } } until ((Get-Date) -gt $deadline)
'NOT_STARTED'
"""

# --- The calling link -------------------------------------------------------
# Phone Link's Bluetooth calling link drops on its own (three times in two
# days). The Calls pane then shows DialerPaneErrorTitle ("We weren't able to
# connect to your mobile device") with a Try again button, and its own advice
# is to toggle Bluetooth off and on. `repair_calling_link` does that from the
# laptop side; `tools/fix_phone_link.py` runs it by hand.

# READY (dial pad with a Call button), BROKEN (the error), NO_WINDOW, UNKNOWN
# (Phone Link open on another tab).
_CALLS_STATE = _UIA + r"""
$win = MainWindow
if (-not $win) { 'NO_WINDOW'; exit 0 }
if (ById $win 'DialerPaneErrorTitle') { 'BROKEN'; exit 0 }
if (ById $win 'ButtonCall') { 'READY'; exit 0 }
'UNKNOWN'
"""

_TRY_AGAIN = _UIA_KEYS + r"""
$win = MainWindow
if (-not $win) { 'NO_WINDOW'; exit 0 }
$btn = ById $win 'DialerPaneErrorActionButton'
if (-not $btn) { 'NO_BUTTON'; exit 0 }
PressEnterOn $win $btn 'DialerPaneErrorActionButton'
"""

# Toggle the laptop's Bluetooth radio through Windows.Devices.Radios (no admin
# rights needed). Prints the final state.
_BLUETOOTH_RADIO = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$type) {
  $task = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
[Windows.Devices.Radios.Radio, Windows.System.Devices, ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus, Windows.System.Devices, ContentType=WindowsRuntime] | Out-Null
$access = Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
if ($access -ne 'Allowed') { "ACCESS_$access"; exit 0 }
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$bt = $radios | Where-Object { $_.Kind -eq 'Bluetooth' } | Select-Object -First 1
if (-not $bt) { 'NO_RADIO'; exit 0 }
Await ($bt.SetStateAsync('__STATE__')) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
Start-Sleep -Milliseconds 500
[string]$bt.State
"""


def calls_state() -> str:
    """READY, BROKEN, NO_WINDOW or UNKNOWN (see _CALLS_STATE); "" if PowerShell failed."""
    return _run_ps(_CALLS_STATE, timeout_s=20)


def bluetooth_radio(state: str) -> str:
    """Set the laptop's Bluetooth radio to "On" or "Off"; returns the state it reports."""
    return _run_ps(_BLUETOOTH_RADIO.replace("__STATE__", state), timeout_s=30)


def repair_calling_link(
    wait_s: float = 60.0,
    off_s: float = 4.0,
    poll_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, list[str]]:
    """
    Bring Phone Link's calling link back. (ready, steps taken) -- never raises.

    Cheapest first: if it is already up, nothing is touched. Then Try again,
    which alone has not fixed it before but costs nothing. Then the laptop's
    Bluetooth radio off and on, which reconnects the phone (and, as a side
    effect, any Bluetooth headphones), then Try again until the dial pad is
    back or `wait_s` runs out. The phone's own Bluetooth is out of reach: if
    this fails, toggling it on the phone is the remaining step.
    """
    steps: list[str] = []
    state = calls_state()
    steps.append(f"Phone Link calls: {state or 'no answer from PowerShell'}")
    if state == "READY":
        return True, steps

    def wait_until_ready(seconds: float) -> bool:
        deadline = clock() + seconds
        while clock() < deadline:
            now = calls_state()
            if now == "READY":
                return True
            if now == "BROKEN":
                _run_ps(_TRY_AGAIN)
            sleep(poll_s)
        return False

    if state == "BROKEN":
        steps.append(f"Try again: {_run_ps(_TRY_AGAIN) or 'no answer'}")
        if wait_until_ready(10.0):
            steps.append("Calling link back after Try again")
            return True, steps

    off = bluetooth_radio("Off")
    steps.append(f"Laptop Bluetooth off: {off or 'no answer'}")
    sleep(off_s)
    on = bluetooth_radio("On")
    steps.append(f"Laptop Bluetooth on: {on or 'no answer'}")
    if on != "On":
        return False, steps
    if wait_until_ready(wait_s):
        steps.append("Calling link back after the Bluetooth toggle")
        return True, steps
    steps.append(f"Still not connected after {int(wait_s)}s: toggle Bluetooth on the phone too")
    return False, steps


def _run_ps(script: str, timeout_s: float = _PS_TIMEOUT_S) -> str:
    """
    Run one PowerShell script; return its last non-empty output line ("" on failure).

    The script goes through a temporary .ps1 file, not stdin: `-Command -`
    reads stdin line by line as if typed, so the multi-line here-string that
    carries the C# audio types silently produces nothing.
    """
    fd, path = tempfile.mkstemp(suffix=".ps1", prefix="california_phone_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as handle:
            handle.write(script)
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("PowerShell call failed: %s", exc)
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if done.returncode != 0 and done.stderr.strip():
        logger.warning("PowerShell error: %s", done.stderr.strip()[:300])
    lines = [line.rstrip() for line in (done.stdout or "").splitlines() if line.strip()]
    if len(lines) > 1:
        # Diagnostics a script printed before its status word (DumpPhone).
        logger.info("PowerShell output:\n%s", "\n".join(lines[:-1]))
    return lines[-1].strip() if lines else ""


def _dial_number(number: str) -> str:
    """Phone Link wants the number as digits with an optional leading +."""
    cleaned = re.sub(r"[^\d+]", "", number or "")
    return cleaned


class PhoneLinkDialer:
    """Dial, detect and end calls through Phone Link's UI."""

    def available(self) -> bool:
        return sys.platform == "win32"

    def dial(self, number: str) -> str:
        """
        Returns "DIALED", or why not: PHONE_NOT_CONNECTED (the Calls pane shows
        "We weren't able to connect to your mobile device" -- the Bluetooth
        calling link is down, seen 2026-10-07), NOT_PREFILLED, NO_BUTTON,
        NO_CALL_WINDOW, or "" when PowerShell itself failed.
        """
        number = _dial_number(number)
        if not number:
            return "BAD_NUMBER"
        self._saw_call = False
        self._gone = 0
        status = _run_ps(_DIAL.replace("__NUMBER__", number))
        if status == "DIALED":
            self._saw_call = True
        logger.info("Phone Link dial %s -> %s", number, status or "<no output>")
        return status

    def in_call(self) -> bool | None:
        """
        True while Phone Link shows a hang-up button; False once a button seen
        in this call is gone (they hung up); None when it cannot tell.

        "No button" alone is never False: on 2026-10-07 a call rang and was
        answered before the button could be seen, and reading its absence as
        "ended" cut the call off before she spoke.
        """
        script = _IN_CALL_DUMP if getattr(self, "_saw_call", False) else _IN_CALL
        status = _run_ps(script, timeout_s=10)
        if status == "YES":
            self._saw_call = True
            self._gone = 0
            return True
        # The button lives inside the main window (no separate call window,
        # recorded live 2026-10-07). Once it has been seen in THIS call, its
        # disappearance is a real hang-up; before that it proves nothing.
        # One missed reading is not enough: a UIA walk can miss the button
        # while Phone Link redraws, and a single miss used to end the call
        # mid-sentence. `_GONE_READINGS` misses in a row (~4-6s) are.
        if status == "NO" and getattr(self, "_saw_call", False):
            self._gone = getattr(self, "_gone", 0) + 1
            logger.info("Phone Link: End button gone (%d/%d)", self._gone, _GONE_READINGS)
            return False if self._gone >= _GONE_READINGS else None
        return None

    def play_sound(self) -> str:
        """
        Ring the phone through Phone Link's "Play sound" (about 20s). Returns
        PLAYING or ALREADY_PLAYING, or why not: NO_WINDOW, NO_BUTTON, DISABLED,
        NO_CONFIRM, NOT_FOREGROUND, NOT_FOCUSED, NOT_STARTED, or "" when PowerShell failed.
        """
        status = _run_ps(_PLAY_SOUND, timeout_s=45)
        logger.info("Phone Link play sound -> %s", status or "<no output>")
        return status

    def hang_up(self) -> str:
        status = _run_ps(_HANG_UP)
        logger.info("Phone Link hang up -> %s", status or "<no output>")
        return status


class MicRoute:
    """
    Context manager: make `endpoint_name` the default microphone, restore after.

    Restores the exact endpoint that was default before, not a hardcoded one,
    and does it in __exit__ so an exception mid-call cannot leave the laptop's
    default mic pointing at the cable.
    """

    _GET = _GET_MIC
    _WHAT = "microphone"

    def __init__(self, endpoint_name: str):
        self.endpoint_name = endpoint_name
        self.previous_id = ""
        self.switched = False

    def __enter__(self) -> "MicRoute":
        target = _run_ps(_FIND_ENDPOINT.replace("__NAME__", self.endpoint_name.replace("'", "''")))
        if not target:
            raise RuntimeError(f"audio endpoint not found: {self.endpoint_name}")
        self.previous_id = _run_ps(self._GET)
        if not self.previous_id:
            # Switching without knowing what to put back would leave the room
            # mic on the cable after the call, and California deaf.
            raise RuntimeError(f"could not read the current default {self._WHAT}")
        # PnP reports the GUID upper-case, IMMDevice lower-case: same device.
        if self.previous_id.lower() == target.lower():
            return self
        if _run_ps(_SET_MIC.replace("__DEVICE__", target)) != "OK":
            raise RuntimeError(f"could not switch the default {self._WHAT}")
        self.switched = True
        logger.info("Default %s -> %s for the call", self._WHAT, self.endpoint_name)
        return self

    def __exit__(self, *exc) -> None:
        if self.switched and self.previous_id:
            if _run_ps(_SET_MIC.replace("__DEVICE__", self.previous_id)) == "OK":
                logger.info("Default %s restored", self._WHAT)
            else:
                logger.error("Could not restore the default %s (%s)", self._WHAT, self.previous_id)
        return None


class SpeakerRoute(MicRoute):
    """
    Context manager: make `endpoint_name` the default SPEAKER for the call, restore after.

    Found 2026-10-08, calling Sergio: Bluetooth headphones ("Black Diamond") had
    connected and become the default output. In a call Windows moves Bluetooth
    headphones to their hands-free profile, a different endpoint from the
    "Headphones" one the loopback was recording, so the callee played where
    California could not hear: 35 seconds of a real person on the line and
    nothing in her ears. The call must not depend on whatever happens to be
    the default output, so for its length Phone Link plays on a fixed device
    and the loopback records that same device by name.
    """

    _GET = _GET_SPEAKER
    _WHAT = "speaker"


def is_windows_with_powershell() -> bool:
    return sys.platform == "win32" and bool(os.environ.get("SystemRoot"))
