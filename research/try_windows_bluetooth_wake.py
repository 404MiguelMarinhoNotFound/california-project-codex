"""One bounded native Windows Bluetooth wake experiment; no sleep or bond removal."""
import ctypes as C
from ctypes import wintypes as W
import concurrent.futures
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1] / "california"
MAC = "9C:12:21:1C:95:AF"


def authenticate():
    class Device(C.Structure):
        _fields_ = [("size", W.DWORD), ("address", C.c_ulonglong),
                    ("device_class", W.ULONG), ("connected", W.BOOL),
                    ("remembered", W.BOOL), ("authenticated", W.BOOL),
                    ("last_seen", W.WORD * 8), ("last_used", W.WORD * 8),
                    ("name", W.WCHAR * 248)]

    class FindParams(C.Structure):
        _fields_ = [("size", W.DWORD)]

    class CallbackParams(C.Structure):
        _fields_ = [("device", Device), ("method", C.c_int),
                    ("capability", C.c_int), ("requirements", C.c_int),
                    ("number", W.ULONG)]

    class ResponseData(C.Union):
        _fields_ = [("number", W.ULONG), ("oob", W.BYTE * 32)]

    class Response(C.Structure):
        _fields_ = [("address", C.c_ulonglong), ("method", C.c_int),
                    ("data", ResponseData), ("negative", W.BYTE)]

    bt = C.WinDLL("bthprops.cpl", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    bt.BluetoothFindFirstRadio.argtypes = [C.POINTER(FindParams), C.POINTER(W.HANDLE)]
    bt.BluetoothFindFirstRadio.restype = W.HANDLE
    bt.BluetoothFindRadioClose.argtypes = [W.HANDLE]
    bt.BluetoothFindRadioClose.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]
    kernel.CloseHandle.restype = W.BOOL
    callback_type = C.WINFUNCTYPE(W.BOOL, C.c_void_p, C.c_void_p)
    bt.BluetoothRegisterForAuthenticationEx.argtypes = [C.POINTER(Device), C.POINTER(W.HANDLE), callback_type, C.c_void_p]
    bt.BluetoothRegisterForAuthenticationEx.restype = W.DWORD
    bt.BluetoothUnregisterAuthentication.argtypes = [W.HANDLE]
    bt.BluetoothUnregisterAuthentication.restype = W.BOOL
    bt.BluetoothAuthenticateDeviceEx.argtypes = [W.HWND, W.HANDLE, C.POINTER(Device), C.c_void_p, C.c_int]
    bt.BluetoothAuthenticateDeviceEx.restype = W.DWORD
    bt.BluetoothSendAuthenticationResponseEx.argtypes = [W.HANDLE, C.POINTER(Response)]
    bt.BluetoothSendAuthenticationResponseEx.restype = W.DWORD
    params = FindParams(C.sizeof(FindParams))
    radio = W.HANDLE()
    search = bt.BluetoothFindFirstRadio(C.byref(params), C.byref(radio))
    if not search:
        raise C.WinError(C.get_last_error())
    bt.BluetoothFindRadioClose(search)
    device = Device()
    device.size = C.sizeof(Device)
    device.address = int(MAC.replace(":", ""), 16)

    @callback_type
    def callback(context, auth_params):
        if "--pair" in sys.argv:
            info = C.cast(auth_params, C.POINTER(CallbackParams)).contents
            code = f"{info.number:06d}"
            print(f"Pair request: method={info.method}, code={code}, address={info.device.address:012X}, capability={info.capability}", flush=True)
            # BLUETOOTH_AUTHENTICATION_METHOD: 1 legacy PIN, 2 OOB, 3 numeric comparison, 4 passkey notification, 5 passkey.
            if info.method != 3 or info.device.address != device.address:
                print("Unsupported authentication method; not automatically approved", flush=True)
                return False
            # Approve on Windows right away: the box auto-confirms in Add-accessory
            # mode and stalls waiting for us. Watch the box in the background in
            # case it does raise a dialog, and confirm it there.
            import threading
            threading.Thread(target=watch_and_confirm_box, args=(code,), daemon=True).start()
            response = Response()
            response.address = info.device.address
            response.method = info.method
            response.data.number = info.number
            rc = bt.BluetoothSendAuthenticationResponseEx(radio, C.byref(response))
            print(f"Windows pair response result={rc}: {C.FormatError(rc).strip()}", flush=True)
            return rc == 0
        # Suppress the pairing wizard. This experiment only tests whether the
        # contact wakes the box; it does not approve or complete a new bond.
        print("Authentication callback received; no pairing response sent", flush=True)
        return True

    registration = W.HANDLE()
    rc = bt.BluetoothRegisterForAuthenticationEx(C.byref(device), C.byref(registration), callback, None)
    if rc:
        kernel.CloseHandle(radio)
        raise C.WinError(rc)
    try:
        print(f"Calling BluetoothAuthenticateDeviceEx once: target={MAC}, structure_size={device.size}, radio_handle_valid={bool(radio.value)}", flush=True)
        started = time.monotonic()
        rc = bt.BluetoothAuthenticateDeviceEx(None, radio, C.byref(device), None, 0)
        print(f"Native result={rc}: {C.FormatError(rc).strip()}; elapsed={time.monotonic()-started:.2f}s", flush=True)
    finally:
        bt.BluetoothUnregisterAuthentication(registration)
        kernel.CloseHandle(radio)


ADB = "C:/platform-tools/adb.exe"
BOX = "192.168.1.84:5555"


def box_ui_nodes():
    """Return (text, bounds, focused) for every labelled node on the box screen."""
    import re
    subprocess.run([ADB, "-s", BOX, "shell", "uiautomator", "dump", "/sdcard/california_pairing.xml"], capture_output=True, timeout=15, check=False)
    xml = subprocess.run([ADB, "-s", BOX, "shell", "cat", "/sdcard/california_pairing.xml"], capture_output=True, text=True, timeout=10, check=False).stdout
    nodes = []
    for m in re.finditer(r'<node[^>]*?text="([^"]*)"[^>]*?focused="(true|false)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        text = m.group(1)
        if text:
            nodes.append((text, tuple(int(x) for x in m.group(3, 4, 5, 6)), m.group(2) == "true"))
    return nodes


def wait_for_box_code(code, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = box_ui_nodes()
        texts = [t.replace(" ", "") for t, _, _ in nodes]
        if any(code in t for t in texts):
            print("Box screen:", [t for t, _, _ in nodes], flush=True)
            return [(t, b, f) for t, b, f in nodes if t.strip().lower() in ("pair", "ok", "confirm", "accept", "yes", "emparelhar")]
        time.sleep(1.5)
    return None


def watch_and_confirm_box(code):
    shown = wait_for_box_code(code, timeout=20)
    if shown is None:
        print("Box never showed a pairing dialog (auto-confirmed or none raised)", flush=True)
        return
    print(f"Box shows matching code; buttons={shown}", flush=True)
    confirm_on_box(shown)


def confirm_on_box(buttons):
    if buttons:
        text, (x1, y1, x2, y2), focused = buttons[0]
        print(f"Confirming on box via tap on '{text}' focused={focused}", flush=True)
        subprocess.run([ADB, "-s", BOX, "shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2)], timeout=10, check=False)
    else:
        print("No explicit confirm button found; pressing DPAD_CENTER", flush=True)
        subprocess.run([ADB, "-s", BOX, "shell", "input", "keyevent", "KEYCODE_DPAD_CENTER"], timeout=10, check=False)


def port_open(ip):
    try:
        with socket.create_connection((ip, 5555), timeout=0.25):
            return ip
    except OSError:
        return None


def scan(prefix):
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        return sorted(filter(None, pool.map(port_open, [f"{prefix}.{i}" for i in range(1, 255)])))


def main():
    state = json.loads((ROOT / "device_state.json").read_text())
    ip = state["devices"]["mibox"]["ip"]
    prefix = ip.rsplit(".", 1)[0]
    before = scan(prefix)
    print(f"Before attempt: TCP 5555 open at {before}; cached box IP={ip}", flush=True)
    if "--pair" in sys.argv:
        log_file = Path(__file__).with_suffix(".log")
        log_stream = log_file.open("w", encoding="utf-8")
        child = subprocess.Popen([sys.executable, "-u", __file__, "--native", "--pair"], stdout=log_stream, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            child.wait(timeout=75)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            print("Pairing stopped at 75-second deadline", flush=True)
        finally:
            log_stream.close()
            print(log_file.read_text(encoding="utf-8"), flush=True)
        print(f"Pair child exit={child.returncode}", flush=True)
        return
    child = subprocess.Popen([sys.executable, "-u", __file__, "--native"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        output, _ = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        output, _ = child.communicate()
        print("Native attempt stopped at 10-second deadline", flush=True)
    print(output, end="", flush=True)
    print(f"Child exit={child.returncode}", flush=True)
    # No CEC wake is sent: it would confound the Bluetooth result.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if port_open(ip):
            break
        time.sleep(2)
    after = scan(prefix)
    print(f"After observation: TCP 5555 open at {after}", flush=True)
    for candidate in after:
        adb = "C:/platform-tools/adb.exe"
        target = f"{candidate}:5555"
        subprocess.run([adb, "connect", target], timeout=5, check=False)
        identity = subprocess.run([adb, "-s", target, "shell", "getprop", "ro.serialno"], capture_output=True, text=True, timeout=5)
        if identity.stdout.strip() == "40152700001181340":
            result = subprocess.run([adb, "-s", target, "shell", "dumpsys", "power"], capture_output=True, text=True, timeout=5)
            print("Verified box:", candidate, [line.strip() for line in result.stdout.splitlines() if "mWakefulness=" in line], flush=True)
        else:
            print(f"Candidate {candidate} is not the configured box", flush=True)
            subprocess.run([adb, "disconnect", target], timeout=5, check=False)


if __name__ == "__main__":
    authenticate() if "--native" in sys.argv else main()
