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

logger = logging.getLogger(__name__)

_PS_TIMEOUT_S = 25

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
  public static string GetDefaultCapture() {
    var e = (IMMDeviceEnumeratorCal)new MMDeviceEnumeratorCal(); IMMDeviceCal d; string id;
    Marshal.ThrowExceptionForHR(e.GetDefaultAudioEndpoint(1, 0, out d)); Marshal.ThrowExceptionForHR(d.GetId(out id)); return id; }
  public static void SetDefault(string id) {
    var p = (IPolicyConfigCal)new PolicyConfigClientCal();
    for (int r = 0; r < 3; r++) Marshal.ThrowExceptionForHR(p.SetDefaultEndpoint(id, r)); }
}
"""

_GET_MIC = "Add-Type -TypeDefinition @'\n" + _AUDIO_TYPES + "\n'@\n[CalAudio]::GetDefaultCapture()"

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

_HANG_UP = _UIA_KEYS + r"""
$e = EndButton
if (-not $e) { 'NO_CALL'; exit 0 }
$pressed = PressEnterOn $e[0] $e[1] ''
if ($pressed -ne 'SENT') { $pressed; exit 0 }
Start-Sleep -Milliseconds 800
if (EndButton) { 'STILL_IN_CALL' } else { 'ENDED' }
"""


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
        status = _run_ps(_IN_CALL, timeout_s=10)
        if status == "YES":
            self._saw_call = True
            return True
        # The button lives inside the main window (no separate call window,
        # recorded live 2026-10-07). Once it has been seen in THIS call, its
        # disappearance is a real hang-up; before that it proves nothing.
        if status == "NO" and getattr(self, "_saw_call", False):
            return False
        return None

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

    def __init__(self, endpoint_name: str):
        self.endpoint_name = endpoint_name
        self.previous_id = ""
        self.switched = False

    def __enter__(self) -> "MicRoute":
        target = _run_ps(_FIND_ENDPOINT.replace("__NAME__", self.endpoint_name.replace("'", "''")))
        if not target:
            raise RuntimeError(f"audio endpoint not found: {self.endpoint_name}")
        self.previous_id = _run_ps(_GET_MIC)
        if not self.previous_id:
            # Switching without knowing what to put back would leave the room
            # mic on the cable after the call, and California deaf.
            raise RuntimeError("could not read the current default microphone")
        # PnP reports the GUID upper-case, IMMDevice lower-case: same device.
        if self.previous_id.lower() == target.lower():
            return self
        if _run_ps(_SET_MIC.replace("__DEVICE__", target)) != "OK":
            raise RuntimeError("could not switch the default microphone")
        self.switched = True
        logger.info("Default microphone -> %s for the call", self.endpoint_name)
        return self

    def __exit__(self, *exc) -> None:
        if self.switched and self.previous_id:
            if _run_ps(_SET_MIC.replace("__DEVICE__", self.previous_id)) == "OK":
                logger.info("Default microphone restored")
            else:
                logger.error("Could not restore the default microphone (%s)", self.previous_id)
        return None


def is_windows_with_powershell() -> bool:
    return sys.platform == "win32" and bool(os.environ.get("SystemRoot"))
