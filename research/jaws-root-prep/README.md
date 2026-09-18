# Xiaomi TV Box S 2nd Gen (jaws / MDZ-28-AA) — root prep, 18 Sep 2026

Prepared but **not executed**: bootloader is still locked, box is stock 772. Everything below was verified on the real box except the destructive steps (unlock onward).

## Why root

The standby-wake whitelist `persist.vendor.wake_up_rc` (currently `C0:5D:39:9C:01:07;` = Xiaomi RC) is written by `/vendor/bin/update_rc_mac`, triggered by init on `property:bluetooth.wake_up_rc=*` (`/vendor/etc/init/hw/init.mitv.common.rc`). Both properties are SELinux-restricted (`vendor_xiaomi_prop`, `exported_bluetooth_prop`); ADB shell cannot set either. Root is required. Even then, a Windows BLE wake is unproven (random LE address, no HID peripheral role) — the more reliable payoff of root is keeping ADB reachable in standby so `KEYCODE_WAKEUP` works.

## Verified facts

- Fingerprint `Xiaomi/jaws/jaws:11/RTT0.211222.001/772:user/release-keys`, security patch 2024-07-05, kernel `5.4.233-android12-9-ga3da9b95f028-ab772`
- A/B, current slot **a**; bootloader `01.01.240418.195646`; `unlocked: no`, `secure: yes`
- OEM unlocking toggle is ON (`sys.oem_unlock_allowed=1`); USB debugging ON
- USB A-to-A cable works: ADB over USB (serial `40152700001181340`) and fastboot (`USB\VID_18D1&PID_0D02`)
- Google USB driver r13 installed on the laptop (pnputil, elevated) — `fastboot devices` works
- No public 772 dump. AndroidDumps has 691/725/737/774 (`https://dumps.tadiphone.dev/dumps/xiaomi/jaws`). Box reports "up to date" — 774 OTA not offered.
- Kernel string embeds the build number, so a 774 boot image must **not** be flashed permanently over 772 vendor modules — only used via `fastboot boot` to obtain root long enough to dump the real 772 partitions.

## Files (not committed — binaries)

Kept locally in the workspace `research/jaws-root-prep/` outside this repo; regenerate as follows if lost:

- Stock 774 `boot.img`: `https://dumps.tadiphone.dev/api/v4/projects/dumps%2Fxiaomi%2Fjaws/repository/files/boot.img/raw?ref=jaws-user-11-RTT0.211222.001-774-release-keys` (md5 `c0d1bf928746d84808488dd713eb48df`).
- Magisk v30.7 APK from GitHub releases; extract `lib/arm64-v8a/libmagiskboot.so`, `libmagiskinit.so`, `libmagisk.so`, `libinit-ld.so` (as `magiskboot`, `magiskinit`, `magisk`, `init-ld`) plus `assets/boot_patch.sh`, `assets/util_functions.sh`, `assets/stub.apk`.
- Push all to `/data/local/tmp/mg`, `chmod 755 *`, `sh boot_patch.sh boot.img` → `new-boot.img` = `magisk_boot_774.img`. Works unrooted.

## Procedure (destructive from step 1)

1. `adb reboot bootloader` → `fastboot flashing unlock` → box factory-resets. (Widevine may drop L1→L3; warranty void.)
2. On the box with the remote: complete Google TV setup, Settings → System → About → click build 7× → Developer options → USB debugging ON, accept the ADB RSA prompt over USB.
3. `adb reboot bootloader` → `fastboot boot magisk_boot_774.img`. If no adb within ~3 min, power-cycle: stock boots unchanged.
4. In the temp-root session (`adb shell su`):
   ```
   for p in boot_a vbmeta_a vbmeta_system_a dtbo_a vendor_boot_a vbmeta_b boot_b; do dd if=/dev/block/by-name/$p of=/sdcard/stock_$p.img; done
   ```
   `adb pull` all of them — these are the only stock 772 backups that will ever exist. Then patch `stock_boot_a.img` with the same on-device Magisk tools → `magisk_boot_772.img`.
5. `adb reboot bootloader`, then:
   ```
   fastboot --disable-verity --disable-verification flash vbmeta_a stock_vbmeta_a.img
   fastboot flash boot_a magisk_boot_772.img
   fastboot reboot
   ```
   `adb install Magisk-v30.7.apk`; verify `adb shell su -c id` → root.
6. Experiment: `su -c 'setprop bluetooth.wake_up_rc "C0:5D:39:9C:01:07;70:9C:D1:07:E4:C7;"'` (triggers `update_rc_mac` as root), confirm `getprop persist.vendor.wake_up_rc`, put box in standby, test wake from Windows. Fallback: keep the box out of deep suspend so Wi-Fi ADB stays up.
7. Afterwards re-enable Wi-Fi ADB for California: `adb tcpip 5555`; reinstall Stremio/Surfshark/YouTube.

Recovery if bricked: Amlogic ADNL via the same cable (`aml_dnl-win32`, start tool before applying power), per XDA thread 4655643.
