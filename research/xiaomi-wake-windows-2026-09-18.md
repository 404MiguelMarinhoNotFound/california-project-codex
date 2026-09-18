# Xiaomi Mi Box wake from a Windows laptop

Research date: 18 September 2026. Constraint: Windows laptop alone, using the existing box, Samsung TV and laptop radios. No Raspberry Pi, ESP32 or additional adapter required by the recommendation.

**Conclusion:** the strongest evidenced solution for this installation is Windows → Samsung TV over the LAN → HDMI-CEC → Xiaomi box. California already implements it. Direct Windows Bluetooth was tested on the hardware the same day and is **ruled out without root** — see the addendum at the end. ADB cannot deliver a wake command once its transport disappears.

This was source research and local code inspection, not a new hardware wake test. Previous hardware measurements below are attributed to the repository. The box's model generation/build was not freshly queried; the repo describes a Mi Box S running Android 11 / SDK 30. Results for newer Xiaomi TV Box generations should not be assumed transferable.

## Evidence and recommendation

| Route | Meets Windows-only constraint? | Evidence for this installation | Decision |
|---|---|---|---|
| Samsung LAN control followed by HDMI-CEC | Yes | Repo records successful end-to-end wakes; code exists | First choice |
| ADB `KEYCODE_WAKEUP` | Yes, while ADB is reachable | Existing code supports shallow standby; deep standby disconnects ADB | Keep as a conditional fast path |
| Native Windows Bluetooth authentication request | Yes | Tested 18 Sep 2026: no wake from standby; pairing while awake is dropped by the box firmware; standby wake is gated by a vendor whitelist that shell cannot edit | Ruled out (see addendum) |
| Windows BLE remote emulation / wake advertisements | Potentially | Windows advertising APIs exist; target wake format and radio behavior unproven | Lower-confidence research |
| Keep Android awake, turn only the TV off | Potentially | Changes standby policy; previous keep-awake settings failed to prevent suspend | Alternative policy, not a wake mechanism |
| Android TV Remote / Google Cast | Yes | Network services cannot be assumed available in deep standby | Not a demonstrated solution |
| MiPower on Linux | No | Explicitly requires Linux and `bluetoothctl` | Outside current scope |
| ESP32 BLE remote | No | Source exists, but behavior is more complicated than repo notes imply | Outside current scope |
| USB HDMI-CEC adapter | Requires new hardware | Supported on Windows, target command still needs testing | Reserve option only |

### 1. Existing Samsung-to-CEC route

The repository's measurements describe the following sequence:

1. Send Wake-on-LAN to the Samsung UE49M5505.
2. Discover/verify its current address, using its stable identity rather than trusting an old IP.
3. Connect to the TV's WebSocket remote interface using its saved pairing token.
4. Change HDMI inputs away and back. The TV emits CEC routing messages that wake the Mi Box.
5. Rediscover the box, wait for ADB to return, and check Android boot readiness.

The repo specifically records that powering on the television alone did not wake the box: changing inputs was essential. It also records roughly 31–32 seconds from completion of the wake action to ADB availability in three trials; that is historical measurement, not a guaranteed current latency.

Implementation: `california/services/media_service.py`, `turn_on()` at line 1160, `_wake_and_wait()` at 1176 and `_wait_for_box()` at 1220. The TV action is `CecWaker.wake()` in `california/services/cec_wake.py` at line 294. Configuration already enables `media.cec_wake`.

A minimal invocation of the existing code, from the `california` directory, is:

```powershell
uv run python -c 'import logging, yaml; from services.media_service import MediaService; logging.basicConfig(level=logging.INFO); config=yaml.safe_load(open("config.yaml", encoding="utf-8")); media=MediaService(config); ok=media.turn_on(); print("Wake completed" if ok else "Wake failed"); raise SystemExit(0 if ok else 1)'
```

This command changes device state; it was not executed during this research. It uses the configured identity/discovery path instead of a hardcoded box IP. It needs the existing project environment with the CEC dependency installed. This invokes box wake only: if the box is already awake, `turn_on()` returns immediately, so it does not by itself guarantee the television is on and showing the correct input.

Two limitations deserve explicit validation. First, `wake()` sends two `KEY_HDMI` presses, so success depends on the starting source and the TV's input cycle. Second, an accepted WebSocket key is not evidence that the TV acted on it: the source already documents Samsung silently ignoring `KEY_HDMI2`. A robust whole-room action should verify box wake and active source independently, and verify TV power where readable.

### 2. The most useful new Windows-only experiment

Microsoft's [`BluetoothAuthenticateDeviceEx`](https://learn.microsoft.com/en-us/windows/win32/api/bluetoothapis/nf-bluetoothapis-bluetoothauthenticatedeviceex) sends an authentication request to a Bluetooth device whose address is supplied in `BLUETOOTH_DEVICE_INFO`. This is a native Win32 API, distinct from the WinRT `PairAsync` and Winsock `AF_BTH` paths named in the repo's failed experiments.

**Inference:** an authentication attempt might cause the controller to contact the sleeping box and trigger a wake, even if pairing ultimately fails. The API documentation establishes the request capability, not the box's response. I found no verified Windows Mi Box implementation and no evidence in the inspected repository that this specific API was tested. That does not prove earlier unpublished attempts omitted it.

The laptop's present-device inventory reports an Intel Wireless Bluetooth adapter with status OK. This confirms hardware is present; it does not establish every required Bluetooth capability.

The relevant comparison is [MiPower](https://github.com/DenizOner/MiPower), which is explicitly Linux-only. Its implementation scans with `bluetoothctl` and issues pairing commands to the target Bluetooth address, then watches for the media player to return. Its README is evidence of the author's intended solution, not a compatibility guarantee for every Xiaomi firmware. Inspected revision: `b27240b7581873a4711d82d4730940a2a03a05c4`.

The [scanner](https://github.com/DenizOner/MiPower/blob/b27240b7581873a4711d82d4730940a2a03a05c4/custom_components/mipower/services/bluetooth/scanner.py) fails if the target address is not found; the [pair sender](https://github.com/DenizOner/MiPower/blob/b27240b7581873a4711d82d4730940a2a03a05c4/custom_components/mipower/services/bluetooth/pair_sender.py) issuing a command does not itself prove wake. These dependencies matter when evaluating claims that knowing a MAC is sufficient.

Proposed experiment: enumerate the real local radio handle; use the box's own recorded Bluetooth address, not its Wi-Fi address or the remote's address; make one bounded authentication attempt; record the Windows result; independently check whether the correct box returns to ADB and reports awake. Run the potentially blocking API in a separate process so it has a hard deadline. Observe any pairing UI. Do not repeatedly toggle power or remove existing bonds. Test while already awake first to establish that the request reaches the intended device, then in controlled standby with the physical remote available for recovery.

A failed authentication result with a successful wake would still be a useful outcome. Conversely, an API success without an independently observed wake is not sufficient.

### 3. Why the ESP32 example is not a drop-in Windows answer

The inspected [BLE remote source](https://github.com/shammysha/esphome-ble-mi-remote/blob/a730c977f2df97f4722a0b5aa9ddf7ada2a715cf/components/ble_mi_remote/ble_mi_remote.cpp) separates three mechanisms: `connectWakeStart()` attempts an outbound BLE connection; `startReconnectAdvert()` / `fireDirectedBurst()` advertise to a bonded peer; and the disconnected power action emits hardcoded manufacturer data. Therefore the local description of `connect_wake` as directed advertising is inaccurate. Source comments reference Mi TV testing, not confirmation on this user's Mi Box. Inspected revision: `a730c977f2df97f4722a0b5aa9ddf7ada2a715cf`.

Windows can transmit manufacturer data using [`BluetoothLEAdvertisementPublisher`](https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.advertisement.bluetoothleadvertisementpublisher?view=winrt-26100), but advertising is policy-controlled and best effort. Its documented surface does not provide the same explicit directed-peer/high-duty-cycle controls used by this ESP32 implementation. Copying manufacturer bytes also does not reproduce a bonded remote's identity or prove that the payload applies to this box. A failed BLE scan of the box does not, by itself, disprove a peripheral-advertising wake mechanism: the remote can advertise while the box listens.

### 4. Routes that should not consume more development time first

**ADB and network remotes.** The repo records `KEYCODE_WAKEUP` failing after sleep because `adbd` was unavailable; intermittent ICMP replies did not imply Android was reachable. Home Assistant independently [documents Xiaomi devices becoming unavailable after power-off and being impossible to turn on through Android TV Remote](https://www.home-assistant.io/integrations/androidtv_remote/). Switching Python libraries or sending a different network key cannot solve that particular transport failure.

**Wake-on-LAN to the box.** No working support is established for this Wi-Fi installation. Lack of Ethernet alone is not a universal proof against wireless wake; firmware and radio wake support are the real requirement. The working magic-packet target here is the Samsung television.

**Wakelocks.** Android [partial wake locks](https://developer.android.com/topic/performance/vitals/excessive-wakelock) normally keep the CPU running with the display off, but this is prevention, not a way to reach an already suspended system. The [Android 11 compatibility definition, section 8.3](https://source.android.com/docs/compatibility/11/android-11-cdd#8_3_power-saving_modes) permits relevant deeper power-state behavior following explicit user inactivation. [Wakelock Revamp issue 11](https://github.com/d4rken-org/wakelock-revamp/issues/11) contains Mi Box Android 9 failure reports, and the [project is archived](https://github.com/d4rken-org/wakelock-revamp). Those old reports are not a fresh Android 11 test, but they invalidate treating this app as a reliable universal fix. The repo already records unsuccessful `wifi_sleep_policy=2` and `stay_on_while_plugged_in=3` experiments.

**IR and external adapters.** Xiaomi's [IR troubleshooting page](https://www.mi.com/ph/support/faq/details/KA-124645/) refers to a second-generation TV Box S, so it does not establish IR support for this device. A [Pulse-Eight USB-CEC adapter](https://www.pulse-eight.com/p/104/usb-hdmi-cec-adap) supports Windows, but adds hardware and still requires target-specific CEC testing. A normal laptop HDMI port should not be assumed to expose CEC. Neither is the first choice under the stated constraint.

## Repo discrepancies and validation plan

`CLAUDE.md` and configuration mention `services/bt_wake.py`, `services/esp32_bt_wake.py` and their use before CEC. Neither module is present in this checkout, and `_wake_and_wait()` calls only `CecWaker`. Enabling the dormant configuration blocks will not implement Bluetooth wake.

Recommended order:

1. Measure the existing CEC route from Windows, including the television starting on another HDMI input and a long standby interval. Separate TV wake time, routing time and box network recovery time.
2. Confirm box identity, `mWakefulness=Awake`, Android readiness and active HDMI source. Do not equate a cached IP, ping reply or accepted command with success.
3. If direct wake without involving the television is required, try the bounded Win32 authentication experiment. Keep CEC as recovery until repeated tests establish reliability.
4. Consider changing the policy to keep the box awake and turn only the television off only if higher idle power and altered CEC behavior are acceptable. This needs its own real-device validation.

No production files, dependencies, device settings or pairings were changed by this research. No wake or sleep command was sent.


## Addendum: hardware test results, 18 September 2026 (evening)

Device confirmed via ADB: manufacturer Xiaomi, `MiTV_AFKR0` ("jaws"), Android 11 build `RTT0.211222.001.772`, Realtek Bluetooth (`/vendor/etc/bluetooth/rtkbt.conf`), Bluetooth address `9C:12:21:1C:95:AF` (Bluetooth name "Living Room TV"), serial `40152700001181340`. Laptop Bluetooth address `70:9C:D1:07:E4:C7` ("LAPTOP-7A9VTGBC"). Script: `research/try_windows_bluetooth_wake.py`.

| Test | Windows result | Box-side evidence | Outcome |
|---|---|---|---|
| `BluetoothAuthenticateDeviceEx` with box in standby | error 258 (timeout) after 5.2 s | ADB never returned in 20 s | No wake; box does not page-scan on classic BT in standby |
| Same call with box awake, no response sent | numeric-comparison callback received | ACL up, `BOND_NONE => BOND_BONDING` | Request reaches the box |
| Full pairing: Windows approves numeric comparison (method 3) | `SendAuthenticationResponseEx` blocks ~30 s, returns 31; auth returns 1244 | `BOND_BONDING` for 30 s, then `bta_dm_authentication_complete_cback ... result: 0x05` (HCI auth failure); **no `sspRequestCallback`, no `PAIRING_REQUEST` broadcast, no `BluetoothPairingDialog`** ever logged, with or without `AddAccessoryActivity` open | Box firmware drops incoming classic pairing between the native stack and the Java layer; nothing on the box can confirm |
| Box-initiated pairing (laptop discoverable, Add-accessory scan 25 s) | — | list stays at "Searching for accessories…" | TvSettings scanner only lists input/audio classes; a laptop is never offered |

Root cause of why pairing would not have helped anyway: the box exposes **`persist.vendor.wake_up_rc = C0:5D:39:9C:01:07;`** — a semicolon-separated whitelist of Bluetooth addresses permitted to wake it from standby, holding only the bonded Xiaomi remote (`C0:5D:39:9C:01:07 [LE] Xiaomi RC`). The vendor `BLE_Service` (`com.droidlogic`, pid 741) is what writes it: on every ACL connect it logs "checking for supported devices after delay", and only recognised remotes (vendor `2717`, product `32b9` firmware files in `/vendor/etc/bluetooth`) qualify. This is the same mechanism the ESP32 project exploits by *spoofing the remote's address* in a directed BLE advertisement.

Attempting to extend the whitelist from ADB fails: `setprop persist.vendor.wake_up_rc` is refused for uid 2000; the box is `ro.secure=1`, `ro.debuggable=0`, verified boot `green`, no `su`. Windows has no supported API to advertise from a spoofed LE address, so the remote-emulation route is also closed on a Windows-only setup.

**Decision:** direct Bluetooth wake from the Windows laptop is not achievable on this box without root. The Samsung → HDMI-CEC route remains the only Windows-only wake. If a direct wake is ever wanted, the requirement is a radio that can transmit from address `C0:5D:39:9C:01:07` (ESP32/Linux HCI), not more Windows Bluetooth work.

No bonds were added or removed; the box was returned to the launcher home screen; temporary UI dumps on `/sdcard` were deleted.


## Addendum 2: root path, CEC benchmark and the USB observation (18 September 2026, night)

### The whitelist can be written, but only as root

`/vendor/etc/init/hw/init.mitv.common.rc` runs `/vendor/bin/update_rc_mac ${bluetooth.wake_up_rc}` as root whenever the non-persistent property `bluetooth.wake_up_rc` changes. Both that trigger (`exported_bluetooth_prop`) and the stored list (`persist.vendor.wake_up_rc`, `vendor_xiaomi_prop`) are refused to the ADB shell by SELinux. `adb root` is refused (production build). So adding the laptop's address needs a rooted box.

### Root is feasible on this exact box, at the cost of a factory reset

Verified on hardware, all non-destructive:

- A USB-A male-to-male cable gives ADB over USB (serial `40152700001181340`) and fastboot (`USB\VID_18D1&PID_0D02`). Google USB driver r13 is now installed on the laptop; `fastboot devices` works.
- Bootloader `01.01.240418.195646`, `unlocked: no`, `secure: yes`, A/B, current slot `a`. OEM-unlocking toggle is ON.
- No public 772 dump exists; AndroidDumps has 691/725/737/774. The box reports "up to date", so 774 is not offered by OTA. The kernel string embeds the build number (`...-ab772`), so a 774 boot image must only be used via `fastboot boot` (temporary) to dump the real 772 partitions, never flashed.
- A Magisk v30.7-patched 774 boot image was produced on-device with `boot_patch.sh` (no root needed) and is kept with the full procedure in `research/jaws-root-prep/README.md`.

`fastboot flashing unlock` forces a factory reset and may downgrade Widevine (Netflix/Prime HD). **Master Miguel declined the reset**, so the box remains stock 772, locked. Even with root, a Windows-originated BLE wake is unproven: the controller matches the whitelist against the LE address it sees, Windows advertises with a random resolvable address, and Windows cannot act as the HID peripheral the remote is.

### CEC wake benchmark

Measured with the repo's own `MediaService` against the real TV and box:

| Path | Condition | Time |
|---|---|---|
| `turn_on()` → ADB `KEYCODE_WAKEUP` | box asleep but still on the LAN | **1.3 s** |
| `cec_waker.wake()` → box `mWakefulness=Awake` | forced CEC path | **52 s** (TV WoL + rediscovery ~11 s; TV up → `KEY_HDMI, KEY_HDMI` at 20.2 s; box awake at 52 s) |

The 52 s matches the "measured at forty-seven seconds" note in the system prompt. The TV had moved to `192.168.1.82` and needed MAC rediscovery, which the waker handled. `is_active_source` was still False right after the wake; not investigated.

### USB tether keeps the box out of deep standby (needs a soak test)

With the USB A-to-A cable connected to the laptop, `turn_off()` (`KEYCODE_SLEEP`) put the box to sleep but it **stayed reachable on TCP 5555 for the full 3-minute observation**, so `turn_on()` took the 1.3 s ADB path. Earlier the same day, unplugged, the box was fully unreachable in standby and only CEC could wake it. Hypothesis: an active USB link (the box in USB device mode) blocks deep suspend. Untested: whether it holds past the box's idle-suspend timer (10–30 min typical), and whether a non-laptop host (e.g. the Samsung's USB port) has the same effect. If it holds, it is a zero-code way to get the ~1 s wake without touching the box.
