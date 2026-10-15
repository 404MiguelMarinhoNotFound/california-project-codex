# firmware/

**Unrelated to California.** Nothing in the assistant imports, builds, flashes or
talks to anything in this folder. It lives here only so the code is not lost.

## atom_govee_switch

A standalone wall switch for the attic Govee H617E strip, running on an
M5Stack Atom Lite (ESP32-PICO-D4) plugged into a USB charger next to the strip.
No laptop, no Wi-Fi.

- **Press the button:** the Atom connects to the strip over Bluetooth LE,
  asks it whether it is on, and sends the opposite.
- **LED:** blue = working, green = now on, dim white = now off,
  red = could not reach the strip. A red is usually the Govee phone app holding
  the strip's single BLE connection.
- If the strip ever stops answering the state query, the Atom toggles from the
  last state it sent, saved in flash.

Protocol, same bytes as `services/govee_service.py`: 20-byte packets on
characteristic `00010203-0405-0607-0809-0a0b0c0d2b11`, 19 command bytes then their
XOR. Power is `33 01 01` / `33 01 00`. State query is `AA 01`, answered on the
notify characteristic `...2b10` as `AA 01 <0|1>`. **The strip does answer it**,
measured 2026-10-15 over 8 presses, which contradicts the "write-only, no notify
characteristic" note in CLAUDE.md.

Measured from the Atom beside the strip: BLE -45 to -51 dBm (the laptop gets
about -82); about 1 s per press.

### Build and flash

PlatformIO through uv, no install needed:

```bash
cd firmware/atom_govee_switch
uvx platformio run -t upload --upload-port COM3
uvx platformio device monitor
```

Notes:
- `huge_app.csv` partitions: the BLE stack alone outgrows the default 1.3 MB app slot.
- One `BLEClient` is created once and reused. Creating and deleting one per press
  crashed the ESP32 BLE stack (`xQueueGenericSend` / spinlock asserts) and rebooted
  the Atom after every other press.
- Board and platform pin from M5Stack's docs:
  <https://docs.m5stack.com/en/core/ATOM%20Lite>,
  <https://docs.platformio.org/en/latest/boards/espressif32/m5stack-atom.html>.
