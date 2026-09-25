# Root on "jaws" (Xiaomi TV Box S 2nd Gen): what it would buy California

Research date: 2026-09-25. **Report only.** Nothing in the repo was changed, and no device state was changed.

**Evidence labels**
- **VERIFIED**: read in source code, a shipped binary or AOSP, with a link.
- **MEASURED**: one of the 2026-09-25 live facts in the brief, or an earlier repo measurement.
- **INFERRED**: my reasoning, not yet tested.

**Live box access:** I could not reach the box at all today. `adb devices` listed nothing, and TCP 5555 on 192.168.1.200 was closed, so it was in deep standby. Instead I read the shipped firmware from the public AndroidDumps copy of **build 774**: `vendor/build.prop`, `wifi.cfg`, `init.mitv.common.rc`, `update_rc_mac`, `btmtk_usb.ko` and `mt7663_usb.ko` (https://dumps.tadiphone.dev/dumps/xiaomi/jaws, ref `jaws-user-11-RTT0.211222.001-774-release-keys`). The box runs 772. These vendor files are very likely the same, but that is not proven.

---

## 0. Bottom line

1. **The biggest wins for "put on <show>" do not need root.** The Stremio launch takes 64-79 s of a 115-131 s total, and almost all of that time is a force-stop, a cold start, `input keyevent` calls that each cost seconds, repeated ADB pings and `uiautomator`. Root changes none of it. Fix it first (section 3, items A1-A3).
2. **A rootless way to stop the TV's `<Standby>` from sleeping the box probably exists, and it is the cheapest route to a ~2 s "turn on the TV".** In AOSP 11, `handleStandby()` only sleeps the device when `isControlEnabled()` is true. `hdmi_control_enabled` is a Global setting the shell can write, and the brief measured that toggling it works. The repo also blames `persist.sys.hdmi.keep_awake` for this, and **AOSP 11 contradicts that**: the property only chooses a wakelock, and it is not read on the `<Standby>` path. Test this before rooting (section 3, item A4).
3. **Wake-on-WLAN after root: plausible, and worth one test session.** The shipped driver is the MediaTek gen4 mt7663 USB driver. Its `wifi.cfg` explicitly sets `Wow 1 / WowEnable 0 / AdvPws 0`, and with those values the driver takes its "suspend Wi-Fi" path instead of the WoW flow. That explains why the box has no ping or ARP in standby. With `Wow=1` the driver already registers USB remote wakeup, and root can flip `WowEnable` at runtime through `/proc/net/wlan/cfg`. What nobody can confirm without testing is whether Amlogic sc2 suspend keeps the USB port powered and treats a USB resume as a wake source. The wake-reason enum has `WIFI_WAKEUP=5`, so the SoC side supports Wi-Fi wake in principle. My estimate is **roughly 40-50%** that it works (section 1).
4. **Bluetooth wake after root: address whitelist, fairly likely to work with a Raspberry Pi.** The shipped `btmtk_usb.ko` is Xiaomi-modified. It logs "MI does not use woble setting" and has `rc_white_list` and "send %d RC address data", so the wake filter is a list of remote addresses. Root is only needed to add an address. The Pi would then advertise from that address, so it needs no spoofing. This is the only route that matches the physical remote (under 1 s). **Estimate: roughly 55-65%.** Which advertisement type is needed is still unknown (section 2).
5. **Before unlocking, back up the `factorydata` partition.** `update_rc_mac` stores the remote whitelist (`bt_rc_mac`) in `/dev/block/factorydata`, a CRC-checked key/value store with a backup copy, and `fastboot flashing unlock` does not re-create it. Back up every by-name partition, not just the boot chain listed in `research/jaws-root-prep/README.md` (section 4).

### Correction to earlier research

`research/xiaomi-wake-windows-2026-09-18.md` says this box has **Realtek** Bluetooth, based on `/vendor/etc/bluetooth/rtkbt.conf`. The live 2026-09-25 logs show `btmtk_usb_unify_woble_suspend`, and `vendor.wlan.def_wifi_chip=mt7663_usb`. The vendor image ships firmware for several chips (rtl8723/8821/8822/8852, MT7961 and MT7663), so `rtkbt.conf` is a shared-image leftover. **On this unit the Bluetooth is MediaTek, on the MT7663 USB combo chip.**

---

## 1. Wake-on-WLAN on this platform

### 1.1 Which driver it is (VERIFIED)
- Platform: `ro.board.platform=sc2` (Amlogic S905X4 family) and `vendor.wlan.def_wifi_chip=mt7663_usb`, from the dump's `vendor/build.prop`.
- Modules shipped: `vendor/lib/modules/mt7663_usb.ko`, `mt7663_usb_prealloc.ko` and `btmtk_usb.ko`.
- The strings in `mt7663_usb.ko` match MediaTek's **gen4-mt7663** driver. It contains the `(REQ STATE) Wow:%d, WowEnable:%d, AdvPws:%d, state:%d` log line (the one captured live), the private commands `SET_WOW_ENABLE`, `SET_WOW_PAR`, `SET_WOW_UDP`, `SET_WOW_TCP`, `WOW_START`, `GET_WOW_REASON` and `GET_WOW_PORT`, and the cfg keys `WowEnable`, `WowHif`, `WowGpioPin`, `WowPinCnt`, `WowScenarioId` and `WowTriigerLevel` (misspelt the same way upstream). It also contains `device_init_wakeup`, several "`... wakeup host`" RX log lines, and **"Wow disable suspend Wifi"**.
- Reference source is the realme open-source drop of the same driver, pinned at commit `a3e43e3d5cbd14caa5cc1a48561180bfc708dbb4`: https://github.com/realme-kernel-opensource/realme8_C25_C25s_Narzo30_Narzo50A_AndroidR_kernel_source/tree/a3e43e3d5cbd14caa5cc1a48561180bfc708dbb4/kernel_modules/connectivity/wlan/core/gen4-mt7663. Its `aisPreSuspendFlow` log prints `state` where jaws prints `AdvPws`, so the revisions are close but not identical. Line numbers below refer to that commit.

### 1.2 The shipped config (VERIFIED, dump `vendor/firmware/mt7663_usb/wifi.cfg`)
```
Wow 1
WowEnable 0
AdvPws 0
GpioInterval 20000
```
This matches the live `cat /proc/net/wlan/cfg` (`Wow|1`, `WowEnable|0`). No `WowHif` or `WowGpioPin` is set, so both take driver defaults.

### 1.3 How the driver uses these keys (VERIFIED)
- `common/wlan_lib.c` ~L6466-6515 (`wlanInitFeatureOption`) reads the keys:
  - `Wow`, default disabled
  - `AdvPws`, default disabled
  - `WowEnable`, default **enabled**. Xiaomi overrode it to 0.
  - `WowHif`, default `ENUM_HIF_TYPE_GPIO`
  - `WowGpioPin`, default `0xFF`
  - `GpioInterval`
- `include/nic_cmd_event.h` L3741-3745 defines `ENUM_HIF_TYPE_SDIO=0`, `USB=1`, `PCIE=2`, `GPIO=3`.
- `common/wlan_lib.c` ~L10141-10160 (`wlanSuspendPmHandle`): the WoW flow runs **only if** `ucWow && (fgWowEnable || ucAdvPws)` and the station is connected. Jaws has `1 && (0 || 0)`, so on suspend the driver never enters WoW. The shipped binary instead logs "Wow disable suspend Wifi", which fits the MEASURED deep-standby behaviour: no ping, no ARP.
- `os/linux/gl_kal.c` L6627 onwards (`kalWowProcess`), inside the start branch ~L6763-6775, builds the WoWLAN command:
  - `ucDetectType = WOWLAN_DETECT_TYPE_MAGIC` (magic packet only)
  - `u2FilterFlag = DROP_ALL | SEND_MAGIC_TO_HOST | ALLOW_1X | ALLOW_ARP_REQ2ME`
  - `bWake` is forced FALSE when `AdvPws==1 && WowEnable==0` (~L6647-6649). **So `AdvPws` alone keeps the link up without waking the host. `WowEnable=1` is the switch that matters.**
- `os/linux/gl_kal.c` ~L8483-8495 (`kalInitDevWakeup`) calls `device_init_wakeup(prDev, TRUE)` whenever `ucWow` is set, with the comment "notify usbcore that we support wakeup function, so usbcore will re-enable our remote wakeup". **So USB remote wakeup (in-band resume signalling) is already registered on jaws, because `Wow=1`.**
- `os/linux/hif/usb/usb.c` L255-300 (`mtk_usb_suspend`) calls `wlanSuspendPmHandle` and then `halPreSuspendCmd`. There is no separate USB-specific WoW step: the chip resumes the bus itself.

### 1.4 Write syntax (VERIFIED)
- **`/proc/net/wlan/cfg`**: `procCfgWrite` prefixes whatever you write with `"set_cfg "` and passes it to `priv_driver_set_cfg`, which calls `wlanoidSetKeyCfg`. That in turn calls `wlanCfgSet` and then **`wlanInitFeatureOption`**, so the change takes effect at once.
  - Source lines: `gl_proc.c` ~L445-470 in the mt7663 drop; `wlan_oid.c` ~L7909-7912.
  - The same code in the gen4 tree for Amazon's Fire TV "mantis": https://github.com/chaosmaster/android_kernel_amazon_mantis/blob/master/drivers/misc/mediatek/connectivity/wlan/gen4/os/linux/gl_proc.c, L476-515.
  - Format is `KEY VALUE`, one per write, for example `echo "WowEnable 1" > /proc/net/wlan/cfg`.
- **`/proc/net/wlan/driver`**: `procDriverCmdWrite` passes the raw string to `priv_driver_cmds` (mantis `gl_proc.c` L544-570). The commands:
  - `SET_WOW_ENABLE <0|1>` sets `fgWowEnable` only (mt7663 `gl_wext_priv.c` L11715-11741).
  - `SET_WOW_PAR <hif> <gpio_pin> <gpio_level> <gpio_timer_ms> <scenario> <block_cnt>` needs more than 3 arguments (L11744 onwards). `hif` takes the `ENUM_HIF_TYPE` values above.
- **Persistence:** these are runtime writes. The driver re-reads `wifi.cfg` whenever Wi-Fi restarts, so a Magisk `service.d` script (or an `on property:` trigger) has to re-apply them every time Wi-Fi comes up. `wifi.cfg` itself lives on read-only, verified `/vendor`, and editing it would break verified boot unless done through a Magisk overlay.
- **Why it fails today:** SELinux blocks the shell from writing `proc_net` (MEASURED). Root removes that block.

### 1.5 The SoC side: Amlogic sc2 suspend (VERIFIED / INFERRED)
- Wake-reason enum, `include/linux/amlogic/pm.h`: `BT_WAKEUP 4`, **`WIFI_WAKEUP 5`**, `CEC_WAKEUP 8`. See https://github.com/khadas/linux/blob/1bd6972cd0093725c0b1dc87f6546648bbb22452/include/linux/amlogic/pm.h L4-15. The live `wakeup_reason:0x4` (remote) and `0x8` (CEC) match this enum exactly (MEASURED + VERIFIED).
- sc2 AOCPU firmware, CoreELEC's copy of Amlogic bl30 (`bl30/src_ao/demos/amlogic/n200/sc2/sc2_ah219/keypad.c` in https://github.com/CoreELEC/u-boot): the reference board maps `GPIO_KEY_ID_WIFI_WAKEUP = GPIOX_7` and reports `WIFI_WAKEUP`. There is also a generic `driver/wifi_bt/wifi_bt_wake.c` with a `wifi_wakeup_task`. **So on Amlogic, Wi-Fi wake is a GPIO line into the always-on CPU, not USB resume.** (VERIFIED for the reference board. Xiaomi's jaws bl30 is closed.)
- Bluetooth wake (0x4) works on jaws, and the btmtk module logs `toggleGPIO`. **INFERRED:** the MT7663 combo's host-wake GPIO is wired to the SoC and jaws' bl30 watches it. Whether the Wi-Fi side has its own wired line, or can use the same one, is unknown. So the WoW test has to try two variants:
  - (a) `WowHif 1` (USB in-band). This works only if the sc2 USB host stays powered in suspend, which is **unlikely** in Amlogic deep suspend (INFERRED).
  - (b) `WowHif 3` (GPIO) with the chip's wake pin. With the default `WowGpioPin 0xFF` the firmware may use its own default pin. That this pin is the one BT uses is INFERRED.
- **Overall likelihood a magic packet wakes jaws after root: roughly 40-50%.** The Wi-Fi side is fully built and deliberately switched off. The Xiaomi board wiring and bl30 are the unknowns.

### 1.6 Test commands, after root (not run; each is reversible with a reboot)
```sh
su -c 'cat /proc/net/wlan/cfg | grep -i wow'
su -c 'cat /sys/bus/usb/devices/*/power/wakeup'          # which USB devices have remote wakeup enabled
su -c 'echo "WowEnable 1" > /proc/net/wlan/cfg'
su -c 'cat /proc/net/wlan/cfg | grep -i wow'             # expect WowEnable|1
# variant A: in-band USB
su -c 'echo "WowHif 1" > /proc/net/wlan/cfg'
# variant B: GPIO (default pin), 20 s interval already in cfg
su -c 'echo "WowHif 3" > /proc/net/wlan/cfg'
su -c 'echo "GET_WOW_REASON" > /proc/net/wlan/driver; cat /proc/net/wlan/driver'
```
Then:
1. Put the box to sleep the normal way (`KEYCODE_SLEEP`) and wait more than 30 s for the force-suspend.
2. From the laptop, send a magic packet for **`9C:12:21:1C:95:AE`** (the box's hardware Wi-Fi MAC since 2026-09-22) to `192.168.1.255:9`. The code in `services/cec_wake.py::_send_wol` (L218-230) can be reused with that MAC.
3. Pass criteria:
   - `dmesg` shows "enter WOW flow" and "CMD_ID_SET_WOWLAN cmd done" at suspend.
   - `cat /sys/class/cec/...` or the CEC dump shows `wakeup_reason:0x5`, or the MTK "`... wakeup host`" RX line appears.
   - ADB returns.
4. **Watch power draw and Wi-Fi stability.** A Wi-Fi radio that stays associated costs idle power.

**What California gains:** a box-only wake from deep standby that does not depend on the TV's 37 s CEC start. Box resume is still roughly 30 s, measured at 52 s through CEC and 1.3 s from shallow standby, so **Wi-Fi wake alone takes the deep-standby path from ~45 s to about the box's resume time**. That saving is real but modest, because resume, not the trigger, dominates (INFERRED from the repo's "box awake at 52s with TV up at 20s").

---

## 2. The Bluetooth wake whitelist

### 2.1 What exists on jaws (VERIFIED from dump binaries)
- `init.mitv.common.rc`:
  - `on property:persist.vendor.wake_up_rc=*` → `setprop bluetooth.wake_up_rc ${persist.vendor.wake_up_rc}`
  - `on property:bluetooth.wake_up_rc=*` → `exec - root root -- /vendor/bin/update_rc_mac ${bluetooth.wake_up_rc}`
  - `update_rc_mac` also runs once as a oneshot service with `seclabel u:r:update_rc_mac:s0`.
- `update_rc_mac` strings: `bt_rc_mac`, `/dev/block/factorydata`, `set_factorydata_by_key`, `get_factorydata_by_key`, `flush_factorydata_partition`, "check factorydata back-up partition CRC fail", `persist.vendor.wake_up_rc`, `FF:FF:FF:FF:FF:FF`, "Update wake_up_rc to %s". **The whitelist is kept in the factorydata partition under key `bt_rc_mac` and mirrored to the property.**
- `btmtk_usb.ko` strings: "MI does not use woble setting", `rc_white_list`, `rc_num_in_whitelist`, "size of white list is %ld,rc num is %d,rc list is %s", "send %d RC address data error ret %d", `BT_RC_VENDOR_T0 or Default`, "passSCAN:0x%02X, enterAPCF:0x%02X, passAPCF:0x%02X, toggleGPIO:0x%02X", "[MI RCW". It also still carries the stock default-filter strings `CRKTM` and `woble_setting_7663.bin`.
- Stock MediaTek reference (not Xiaomi-modified), Amlogic-Lineage `hardware/amlogic/bluetooth` `mtk/mtkbt/bt_driver_usb/btmtk_usb_main.c` @ `8a1a4bde3be7e4196f288867fe74b8fc32b2f2a1`, L2128-2185 `btmtk_usb_set_Woble_APCF`: without a woble setting file, the stock driver programs an APCF (vendor HCI `0xFD57`) manufacturer-data filter built from the box's own BD address plus the tag `CRKTM`. Xiaomi replaced this ("MI does not use woble setting") with an **address list sent to the controller**.

### 2.2 What that means for a fixed-address ESP32 or Pi (INFERRED)
- The controller is loaded with RC **addresses**. The "passSCAN → enterAPCF → passAPCF → toggleGPIO" stages suggest this sequence: the controller scans while the host sleeps, the address filter matches, and the chip toggles its host-wake GPIO (reported as `wakeup_reason 0x4`).
- **What to send after adding the address:** a BLE advertisement from exactly that address. The earlier ESP32 work (shammysha `ble_mi_remote`, `fireDirectedBurst` / `connectWakeStart`, see the 2026-09-18 research doc) wakes with a **directed advertisement (ADV_DIRECT_IND)** aimed at the box's address (`9C:12:21:1C:95:AF`), sent from the remote's address. That is the most likely requirement. A plain ADV_IND is the fallback to test.
- **Address type:** the list stores 6-byte strings. A Pi's Broadcom controller advertises from its **public** address when told to (`hciconfig hci0 noleadv; hcitool -i hci0 cmd 0x08 0x0006 …` with own-address type 0x00), so it can present a fixed, whitelistable address. Windows cannot, as found on 2026-09-18.
- Test plan after root:
  1. `su -c 'setprop persist.vendor.wake_up_rc "C0:5D:39:9C:01:07;<PI_BDADDR>;"'`. Setting the persist property fires both init triggers. Then confirm that `getprop persist.vendor.wake_up_rc` and `dmesg | grep -i "rc num"` show 2 entries.
  2. Sleep the box and wait more than 30 s. From the Pi, send about 1 s of high-duty ADV_DIRECT_IND at `9C:12:21:1C:95:AF`. If that fails, try ADV_IND with the RC's manufacturer data copied from a sniff (`btmon` while pressing the real remote).
  3. Pass: `wakeup_reason:0x4` and ADB back.
- **Risk:** `update_rc_mac` rewrites the factorydata partition, a unique per-device store that also backs other factory keys (INFERRED from its generic key/value API). **Back up factorydata first.** A malformed list could unpair the real remote's wake. Keep `C0:5D:39:9C:01:07;` first in every list.
- **What California gains:** the only mechanism that matches the remote. The box resumes as it does from the remote (under 1 s, per the brief), and One Touch Play then turns the TV on. It needs the production Pi's radio, which the project is already moving to.
- **Likelihood: roughly 55-65%.** The mechanism is confirmed as address-based. The advertisement type is not.

---

## 3. Other wins, rootless and root-enabled

### Rootless (do these first)

**A1. Stop force-stopping Stremio on every play.** *Benefit: very high. Effort: low. Risk: low.*
- `services/stremio_service.py` L579-581 calls `media_service.force_stop_app("stremio")` and then `sleep(0.3)` before every deep link, so every "put on X" pays a cold start (MEASURED: the launch is 64-79 s of 115-131 s).
- Change: send the deep link to the running app. Only force-stop when the app is known to be stuck, for example when the second deep link does not bring it to the foreground.
- `_wait_for_stremio_foreground` (L707) already tells you whether it responded.

**A2. Cut the ADB process-spawn tax.** *Benefit: high. Effort: medium.*
- `MediaService.keyevent` (`services/media_service.py` L965-968) calls `ensure_connected()` (L662), which runs `adb shell echo ping`, and then a second `adb shell input keyevent`. That is two process spawns plus an `app_process` start on the box. The brief MEASURED one OK press at 5.7 s.
- Fixes:
  - (i) Batch keys in one call: `input keyevent 23 23` is accepted by Android's `input` command. INFERRED; verify.
  - (ii) Keep one long-lived `adb shell` and write commands to its stdin instead of spawning `adb.exe` each time.
  - (iii) Skip the `ensure_connected()` ping inside hot loops: `_wait_for_playback` (`stremio_service.py` L719) and `_is_playing` (L962).
- None of this needs root. **A resident on-box daemon only adds value if (ii) is not enough.**

**A3. Make `uiautomator` the exception.** *Benefit: high on the scan-fallback path.*
- `_dump_ui_hierarchy` (`stremio_service.py` L820) and `MediaService.dump_ui_hierarchy` (L919) hang while video renders (CLAUDE.md, "Known Bugs"). The 45 s scan cap is still paid.
- Treat the first `state=3` from `_is_playing` as success, and do not start the provider scan until two OK presses have failed and `media_session` shows no new stamp. The `updated=` stamp logic already exists for YouTube (`_is_new_playback`, `media_service.py` L322).
- Also remove the double library sync the benchmark saw: `_sync_library_for_resume` (L531) is followed by another sync.

**A4. `hdmi_control_enabled=0` as the TV-only-standby guard.** *Benefit: very high, since "turn on" becomes ~2 s every time. Effort: low. Risk: medium.*
- AOSP 11, `HdmiCecLocalDevice.handleStandby()` (https://github.com/aosp-mirror/platform_frameworks_base/blob/android11-release/services/core/java/com/android/server/hdmi/HdmiCecLocalDevice.java L527-537) calls `mService.standby()` **only if `mService.isControlEnabled()`** (VERIFIED). `isControlEnabled` follows Global `hdmi_control_enabled`, which the shell can write; the brief MEASURED toggling it.
- The idea: in `_standby_tv_only` (`media_service.py` L1284-1335), set `hdmi_control_enabled 0` **before** `cec_waker.standby_tv()`. The TV's `<Standby>` is then ignored, and the box stays awake without the 3-5 s catch-and-rewake race in `_catch_the_box_before_it_suspends` (L1349).
- On `turn_on`, set it back to 1. The box then broadcasts `<Active Source>` (MEASURED: the TV switches in ~1 s when it is on). Whether re-enabling also sends `<Text View On>` to a TV in standby is **untested**. If not, keep the `KEY_HDMI` pair / WoL TV half (`_confirm_tv_showing_box` L1476, `cec_wake.press_input_pair` L305).
- Risks:
  - While CEC is disabled, the physical remote's CEC features (TV volume passthrough, One Touch Play) are off.
  - A crash between the disable and the re-enable leaves CEC off. The fix is a startup check that re-enables it.
- Add a bench mode next to `tools/bench_tv_power.py::cmd_standby_mode` (L237).

**A5. Block OTA and debloat.** *Benefit: protects every other change.*
- `pm disable-user --user 0 <xiaomi updater pkg>`. The package name was not found; run `pm list packages | grep -iE "ota|update"` when the box is up.
- After root this becomes important: an OTA onto a Magisk-patched boot will either fail or remove root.

### Root-enabled

**R1. `persist.adb.tcp.port=5555`.** *Benefit: reliability, not speed. Effort: trivial. Risk: low.*
- Today it is empty (MEASURED), so after any reboot `_classify_failure` (`media_service.py` L711-745) hits `no_adb_port` and needs USB.
- As root: `setprop persist.adb.tcp.port 5555`. AOSP adbd reads it at start. VERIFIED as the standard mechanism, not checked in jaws' adbd.
- No code change. Update the `no_adb_port` spoken line afterwards.

**R2. Bluetooth wake whitelist** (section 2). *Benefit: highest ceiling. Effort: medium, and needs the Pi.*
- Code: a new waker next to `CecWaker`, for example `services/ble_wake.py` driving the Pi's HCI. It would be called first in `MediaService._wake_and_wait` (L1527), with CEC as the fallback.
- `turn_on` (L1230) keeps its `None` branch. The only change is that the `None` branch tries BLE before CEC.

**R3. Wake-on-WLAN** (section 1). *Benefit: medium, and it needs no extra hardware.*
- Code: the magic packet is `CecWaker._send_wol` (L218) with the box MAC. Add `media.box_mac` to config and send it in `_wake_and_wait` before the CEC chain.
- A Magisk `service.d` script re-applies `WowEnable 1` (+`WowHif`) whenever `wlan0` comes up.

**R4. Defeat the force-suspend / keep the box shallow.** *Benefit: same outcome as A4 (~2 s wake always). Effort: medium. Risk: medium, because of idle power.*
- Android 11 added a hidden `PowerManager.forceSuspend()` that suspends regardless of wakelocks (INFERRED from AOSP 11 API history; the caller on jaws was not identified). The live log line "PowerManagerService: force-suspend now" fits this.
- With root:
  1. Find the caller: `logcat -b all | grep -i force-suspend`, then check `dumpsys power` for the requesting uid.
  2. Disable that Xiaomi/droidlogic component.
  3. Or hold a kernel wakelock (`echo california > /sys/power/wake_lock`). That does not stop a forced write to `/sys/power/state`.
- Prefer A4 if it works: it needs no root and has fewer side effects.

**R5. Direct CEC transmission.** *Benefit: low-medium. Effort: medium.*
- The Amlogic `hdmi_ao_cec` driver exposes a CEC device node. CoreELEC `common_drivers/drivers/media/cec/` has the `wakeup_reason` dump (`hdmi_cec_dump.c`). Writing raw frames as root (for example `40 04` <Text View On>, `4F 82 20 00` <Active Source>) would let the box turn the TV on in one step without the Samsung websocket or WoL.
- The raw-write ABI on jaws was **not verified**.
- Only helps while the box is awake. If A4 works, the `hdmi_control_enabled` toggle already gives `<Active Source>` without root.

**R6. `persist.sys.hdmi.keep_awake`: do not bother.**
- AOSP 11 `HdmiCecLocalDevicePlayback.java` L50-55 and L224-243 (`Constants.java` L438) uses it **only** to pick a `SystemWakeLock` or a dummy lock while the box is the active source. It is not consulted on the `<Standby>` path (`handleStandby` above).
- The firmware's force-suspend ignores wakelocks anyway (MEASURED).
- The docstring at `media_service.py` L1297-1300 and CLAUDE.md's "gated by `persist.sys.hdmi.keep_awake`" are **contradicted by AOSP 11**, unless droidlogic patched the framework, which was not checked.

**R7. On-box resident daemon** (a Magisk `service.d` script with a toybox `nc -l` loop, or a small binary). *Benefit: medium. Risk: security.*
- It would remove the `adb connect`/`ping` costs and could push `media_session` changes instead of polling them.
- It is an unauthenticated root shell on the LAN unless it filters on the Pi's IP. Only worth building if A2(ii) falls short.

**R8. Keep Stremio resident** (root: `oom_score_adj`, or a Magisk module making it persistent). *Benefit: small once A1 is done.* Android TV rarely kills the foreground-recent app.

**R9. Reading the TV input without log parsing.** No clean route was found. The CEC driver's state is in `dumpsys hdmi_control`, which is already used (`hdmi_state` L1642). Not worth root.

### Ranking (benefit vs effort and risk)

| # | Item | Root? | Benefit to the objective | Effort | Risk |
|---|---|---|---|---|---|
| 1 | A1 no force-stop | no | "put on X" -30-60 s (INFERRED) | low | low |
| 2 | A4 `hdmi_control_enabled` guard | no | "turn on" 45 s → ~2 s | low | medium |
| 3 | A2 batch/persistent adb, fewer pings | no | seconds per key, per poll | medium | low |
| 4 | A3 scan only as last resort, one sync | no | removes 45 s worst case | low | low |
| 5 | R1 `persist.adb.tcp.port` | yes | reliability after reboots | trivial | low |
| 6 | R2 BLE whitelist + Pi | yes | remote-like wake, even from deep standby | medium | medium (factorydata) |
| 7 | R3 WoWLAN | yes | deep-standby wake without the TV | low to test | low-medium |
| 8 | R4 defeat force-suspend | yes | same as A4 if A4 fails | medium | medium |
| 9 | R5 raw CEC | yes | small | medium | low |
| 10 | A5 block OTA / R7 daemon / R8 resident | mixed | protective or small | low-med | low-med |

---

## 4. Safety and backup

- **What unlock wipes (VERIFIED from secondary sources, not primary):**
  - `fastboot flashing unlock` triggers a factory reset, which wipes userdata. The jaws guide also runs `fastboot flashing unlock_critical` (https://gist.github.com/supechicken/3c8378be3469bc2f82b7b319f202ed82).
  - It does not touch the eMMC's factorydata, env or Amlogic unifykey areas (INFERRED; a normal AOSP unlock only wipes userdata/metadata).
- **Unique data to back up first**, in the temporary-root session of `research/jaws-root-prep/README.md` step 4:
  - `ls -l /dev/block/by-name/`, then `dd` **every** partition except `userdata`, and at minimum `factorydata`, `boot_a/b`, `vbmeta*`, `dtbo*`, `vendor_boot*` and `misc`.
  - If present, also `param`, `tee` and `rsv`. The partition list was not read, because the box was offline.
  - `factorydata` is VERIFIED to hold `bt_rc_mac` in a CRC-checked store with a backup copy. The Wi-Fi/BT MAC and serial probably also live there or in unifykey (INFERRED).
- **Widevine after unlock:** the jaws gist says "Widevine L1 will still work" (secondary, unverified). The 2026-09-18 README warned it "may" drop to L3. Check with a DRM info app before and after.
- **Restore path:**
  - A stock Xiaomi full OTA zip, sideloaded from stock recovery, is signature-checked against Xiaomi's release keys, so it works whatever the bootloader state. That is standard AOSP recovery behaviour, not tested on jaws.
  - **No official Xiaomi download for 772 or 774 was found.** The jaws gist sources OTA payloads from 4pda (untrusted mirror). AndroidDumps has 774 partition dumps, but those are not a signed OTA.
  - Relock only with fully stock images, or it bricks.
- **Last-resort recovery:** Amlogic USB burning mode / ADNL over the same USB A-to-A cable (XDA thread 4655643, the debrick case for this exact model). Prerequisites:
  - the vendor burn tool (`aml_dnl`), started before power is applied
  - a full burn image, which **was not found publicly for jaws**. The debrick in that thread is the only evidence it can be done.
- **Existing root-prep caveat still holds:** use the 774 boot image only through `fastboot boot`, then patch the dumped 772 `boot_a`. The kernel string embeds `-ab772`.

---

## 5. Where California changes (file:line)

| Item | Sites |
|---|---|
| A1 | `services/stremio_service.py` L579-581 (force-stop), L586 (autoplay sleep), L700-705 `_try_stremio_autoplay`, L707 `_wait_for_stremio_foreground` |
| A2 | `services/media_service.py` L544 `_adb`, L662 `ensure_connected`, L965-968 `keyevent`; `services/stremio_service.py` L962 `_is_playing`, L1012 `_keyevent`, L1005 `_run_shell` |
| A3 | `services/stremio_service.py` L531 `_sync_library_for_resume`, L563-670 `_play_deep_link`, L820 `_dump_ui_hierarchy`; `media_service.py` L322 `_is_new_playback` (reuse) |
| A4 | `services/media_service.py` L1268-1282 `turn_off`, L1284-1335 `_standby_tv_only`, L1337-1347 (OTP helpers as the template for an `hdmi_control_enabled` helper), L1349 `_catch_the_box_before_it_suspends`, L1230 `turn_on`, L1476 `_confirm_tv_showing_box`; `core/orchestrator.py` L308 `_ensure_playable`, ~L736-777 turn_on/turn_off dispatch; `tools/bench_tv_power.py` L237 `cmd_standby_mode` |
| R1 | none in code; update the spoken fix in `media_service.py` ~L739 |
| R2/R3 | `services/media_service.py` L1527 `_wake_and_wait` (try BLE / WoWLAN before `cec_waker.wake()`), L1575 `_wait_for_box` (unchanged); `services/cec_wake.py` L218 `_send_wol` (factor out for the box MAC); `config.yaml` new `media.box_mac` / `ble_wake` block; `tools/bench_tv_power.py` L396 `cmd_wake` for timing |
| R4 | on-box only; `_catch_the_box_before_it_suspends` could be deleted if it works |
| R6 | doc-only fix: `media_service.py` L1297-1300 docstring and CLAUDE.md's `keep_awake` claim |

## Not found / unverified
- The jaws 772 partition list, and whether `WowHif`/`WowGpioPin` are wired on this board (box offline).
- The jaws bl30 wake-source mask.
- The advertisement type Xiaomi's RC whitelist requires.
- The raw write ABI of the Amlogic CEC node.
- The name of the Xiaomi OTA package, and who calls `forceSuspend`.
- An official OTA download URL, and a public burn image.
