"""
Measure the room's power path against the real TV and box.

Every number in the power section of CLAUDE.md came from a one-off script, and
the questions keep coming back: how long does the box stay reachable after
KEYCODE_SLEEP, does its own wake turn the television on, and how long does
turn_on() take end to end. This tool answers them repeatably, with the same
MediaService/CecWaker the assistant uses, so a measurement here is the
behaviour she will get.

    uv run python tools/bench_tv_power.py soak --minutes 60
    uv run python tools/bench_tv_power.py otp
    uv run python tools/bench_tv_power.py wake --runs 3 --between-minutes 2
    uv run python tools/bench_tv_power.py wake --runs 3 --via-turn-on
    uv run python tools/bench_tv_power.py keys      # the proven KEY_HDMI pair, TV-on from shallow standby
    uv run python tools/bench_tv_power.py wol-only  # WoL alone, box awake
    uv run python tools/bench_tv_power.py tv-standby --key KEY_POWER   # one press, outcome read off the bus
    uv run python tools/bench_tv_power.py standby-mode --hold 120      # the whole tv_only_standby round trip

Never sends KEYCODE_POWER -- it is a toggle, see CLAUDE.md. `soak` and `wake`
put the room to sleep on purpose; run them when nobody is watching.
"""

import argparse
import logging
import re
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.device_finder import _ips_for_mac, _read_arp_table, port_open  # noqa: E402
from services.media_service import (  # noqa: E402
    MediaService,
    _parse_active_source,
    _parse_tv_power,
    _parse_wakefulness,
)

_CEC_LINE_RE = re.compile(r"^\s*\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}")


def _stamp(t0: float) -> str:
    return f"t=+{time.monotonic() - t0:5.1f}s"


def _wakeup(svc: MediaService) -> bool:
    # A no-op when already awake; never KEYCODE_POWER.
    return svc._adb("shell input keyevent KEYCODE_WAKEUP")[0]


def _dump_hdmi(svc: MediaService) -> str:
    ok, out = svc._adb("shell dumpsys hdmi_control")
    return out if ok and out else ""


def _cec_lines(dump: str) -> list[str]:
    return [line.rstrip() for line in dump.splitlines() if _CEC_LINE_RE.match(line)]


def _watch_tail(svc: MediaService, t0: float, seconds: float, seen: set[str]) -> dict:
    """
    Poll hdmi_control once a second, print every CEC line the box has not
    logged before, and record when tv_power/active_source first flip.
    """
    firsts: dict = {"tv_on": None, "active": None, "tv_standby": None}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        dump = _dump_hdmi(svc)
        for line in _cec_lines(dump):
            if line not in seen:
                seen.add(line)
                print(f"  {_stamp(t0)} cec | {line.strip()}", flush=True)
        power = _parse_tv_power(dump) if dump else None
        active = _parse_active_source(dump) if dump else None
        if power == "on" and firsts["tv_on"] is None:
            firsts["tv_on"] = time.monotonic() - t0
            print(f"  {_stamp(t0)} tv_power=on", flush=True)
        if power == "standby" and firsts["tv_standby"] is None:
            firsts["tv_standby"] = time.monotonic() - t0
        if active is True and firsts["active"] is None:
            firsts["active"] = time.monotonic() - t0
            print(f"  {_stamp(t0)} active_source=True", flush=True)
        time.sleep(1)
    return firsts


def _room(svc: MediaService) -> str:
    awake = svc.is_awake()
    hdmi = svc.hdmi_state() if awake is not None else None
    return (f"box awake={awake} at {svc.target}"
            + (f", tv_power={hdmi.tv_power}, active_source={hdmi.active_source}" if hdmi else ""))


def cmd_soak(svc: MediaService, args) -> int:
    if svc.is_awake() is not True:
        print("Precondition: the box must be awake and reachable.")
        return 2
    ok, power = svc._adb("shell dumpsys power")
    locks = power.split("Suspend Blockers")[0].split("Wake Locks:")[-1] if ok else ""
    print("Baseline:")
    print("  " + "\n  ".join(line.strip() for line in locks.strip().splitlines()[:8]))
    for key in ("global stay_on_while_plugged_in", "global hdmi_control_auto_device_off_enabled"):
        print(f"  {key} = {svc._adb(f'shell settings get {key}')[1].strip()}")
    ip = svc.ip
    print(f"Sleeping the room via turn_off() -> {svc.turn_off()}")
    t0 = time.monotonic()
    misses = 0
    last = None
    deadline = t0 + args.minutes * 60
    while time.monotonic() < deadline:
        open_ = port_open(ip, svc.port, 0.5)
        awake = None
        if open_:
            ok, out = svc._adb("shell dumpsys power")
            awake = _parse_wakefulness(out) if ok else None
        in_arp = bool(_ips_for_mac(_read_arp_table(), svc.mibox_mac)) if svc.mibox_mac else None
        state = (open_, awake, in_arp)
        if state != last:
            print(f"{_stamp(t0)} port5555={'open' if open_ else 'closed'} awake={awake} arp={in_arp}", flush=True)
            last = state
        misses = 0 if open_ else misses + 1
        if misses >= args.lost_after:
            print(f"Reachability lost at {_stamp(t0)} (ARP entry {'still present' if in_arp else 'gone'}).")
            return 0
        time.sleep(args.interval)
    print(f"Still shallow after {args.minutes} min: port open, awake={last[1] if last else None}.")
    return 0


def cmd_otp(svc: MediaService, args) -> int:
    print("Room:", _room(svc))
    awake = svc.is_awake()
    if awake is not False:
        print("Precondition: the box must be asleep but reachable (is_awake() is False).")
        print("Run `soak` first, or `adb shell input keyevent KEYCODE_SLEEP` and wait a few seconds.")
        return 2
    seen = set(_cec_lines(_dump_hdmi(svc)))
    print(f"Snapshot: {len(seen)} CEC lines. Sending KEYCODE_WAKEUP only (no WoL, no keys)...")
    t0 = time.monotonic()
    print(f"  {_stamp(t0)} wakeup sent -> {_wakeup(svc)}")
    firsts = _watch_tail(svc, t0, args.watch, seen)
    print(f"Result: box awake={svc.is_awake()}; first tv_power=on at {firsts['tv_on']}, "
          f"first active_source at {firsts['active']} (None = never within {args.watch}s).")
    print("Look at the television: did it turn on, and is it showing the box?")
    return 0


def cmd_keys(svc: MediaService, args) -> int:
    print("Room:", _room(svc))
    waker = svc.cec_waker
    if not waker.resolve_tv_ip():
        print("TV not reachable over REST; the key needs an address.")
        return 2
    seen = set(_cec_lines(_dump_hdmi(svc))) if svc.is_awake() is not None else set()
    keys = [waker.input_key] * args.presses
    print(f"Sending {keys} over the websocket (TV at {waker._tv_ip})...")
    t0 = time.monotonic()
    result = waker._send_keys(keys)
    print(f"  {_stamp(t0)} keys -> ok={bool(result)} detail={result.detail!r} needs_pairing={result.needs_pairing}")
    if svc.is_awake() is not None:
        firsts = _watch_tail(svc, t0, args.watch, seen)
        print(f"Result: first tv_power=on at {firsts['tv_on']}, first active_source at {firsts['active']}.")
    else:
        print("Box unreachable, so no CEC tail to read; judge the television by eye.")
    return 0


def cmd_wol_only(svc: MediaService, args) -> int:
    print("Room:", _room(svc))
    waker = svc.cec_waker
    seen = set(_cec_lines(_dump_hdmi(svc))) if svc.is_awake() is not None else set()
    t0 = time.monotonic()
    waker._send_wol()
    print(f"  {_stamp(t0)} WoL sent to {waker.tv_mac}")
    if svc.is_awake() is not None:
        firsts = _watch_tail(svc, t0, args.watch, seen)
        print(f"Result: first tv_power=on at {firsts['tv_on']}, first active_source at {firsts['active']}.")
    else:
        deadline = time.monotonic() + args.watch
        while time.monotonic() < deadline:
            if waker.resolve_tv_ip(force=True):
                print(f"  {_stamp(t0)} TV answers REST at {waker._tv_ip} (answering is not powered on)")
                break
            time.sleep(2)
    return 0


def cmd_tv_standby(svc: MediaService, args) -> int:
    """
    Send ONE key to the television over its websocket and read what the CEC
    bus says happened: <Standby> broadcast = it went off; enumeration traffic
    = it just came ON (so a toggle found it off); nothing = unconfirmed. The
    box must stay awake throughout, which is the whole point of the mode.
    """
    print("Room:", _room(svc))
    if svc.is_awake() is not True:
        print("Precondition: the box must be awake and reachable.")
        return 2
    before = svc.hdmi_state()
    if before.tv_power != "on" and not args.force:
        print(f"The bus does not say the TV is on (tv_power={before.tv_power}); "
              f"a toggle now could turn it ON. Re-run with --force if you can see it is on.")
        return 2
    if not svc.cec_waker.resolve_tv_ip():
        print("TV not reachable over REST; the key needs an address.")
        return 2
    print(f"TV at {svc.cec_waker._tv_ip}")
    t0 = time.monotonic()
    result = svc.cec_waker._send_keys([args.key])
    print(f"  {_stamp(t0)} {args.key} -> ok={bool(result)} detail={result.detail!r}", flush=True)
    if not result:
        return 1
    verdict = None
    while time.monotonic() - t0 < args.watch:
        time.sleep(1.5)
        h = svc.hdmi_state(since=before.newest_stamp)
        awake = svc.is_awake()
        print(f"  {_stamp(t0)} tv_power={h.tv_power} active_source={h.active_source} box_awake={awake}", flush=True)
        if h.tv_power == "standby":
            verdict = "TV went to STANDBY (Standby broadcast seen)"
            break
        if h.tv_power == "on":
            verdict = "TV came ON (enumeration traffic) -- it was off before the press"
            break
    print("Verdict:", verdict or f"no TV evidence on the bus within {args.watch}s (unconfirmed)")
    print("Box awake at the end:", svc.is_awake())
    return 0


def cmd_standby_mode(svc: MediaService, args) -> int:
    """
    The whole tv_only_standby round trip on the real room: turn_off() puts only
    the television to sleep, the box is caught before it suspends, it stays
    awake for `--hold` seconds, and then turn_on() is timed.

    Forces media.power.tv_only_standby on for this run so the shipped config
    does not have to be edited to measure it.
    """
    print("Room:", _room(svc))
    if svc.is_awake() is not True:
        print("Precondition: the box must be awake and reachable.")
        return 2
    before = svc.hdmi_state()
    if before.tv_power != "on":
        print(f"The bus does not say the TV is on (tv_power={before.tv_power}); "
              f"turn_off would refuse to send a toggle. Turn the TV on first.")
        return 2

    svc.tv_only_standby = True
    otp_before = svc._one_touch_play_setting()
    print(f"one_touch_play before: {otp_before}")

    t0 = time.monotonic()
    ok = svc.turn_off()
    print(f"  {_stamp(t0)} turn_off() -> {ok}; last={svc.last_wake_result}", flush=True)
    print(f"  {_stamp(t0)} one_touch_play restored to: {svc._one_touch_play_setting()}")

    deadline = time.monotonic() + args.hold
    worst = None
    while time.monotonic() < deadline:
        awake = svc.is_awake()
        hdmi = svc.hdmi_state(since=before.newest_stamp) if awake is not None else None
        line = (f"  {_stamp(t0)} box_awake={awake}"
                + (f" tv_power={hdmi.tv_power}" if hdmi else ""))
        print(line, flush=True)
        if awake is not True:
            worst = awake
        time.sleep(args.interval)

    held = svc.is_awake()
    print(f"After {args.hold:.0f}s: box_awake={held} (worst seen: {worst})")
    print("Look at the television: it should be OFF and have stayed off.")

    t1 = time.monotonic()
    on = svc.turn_on()
    print(f"turn_on() -> {on} in {time.monotonic()-t1:.1f}s; last={svc.last_wake_result}")
    hdmi = svc.hdmi_state()
    print(f"final: tv_power={hdmi.tv_power} active_source={hdmi.active_source} "
          f"one_touch_play={svc._one_touch_play_setting()}")
    return 0


def _time_primitives(svc: MediaService, watch: float) -> dict:
    """Box half and TV half in parallel, timing every first."""
    t0 = time.monotonic()
    firsts = {"box_awake": None, "tv_rest": None, "tv_on": None, "active": None}

    def box_half():
        _wakeup(svc)

    def tv_half():
        if svc.cec_waker.power_on_tv():
            firsts["tv_rest"] = time.monotonic() - t0

    threads = [threading.Thread(target=box_half, daemon=True), threading.Thread(target=tv_half, daemon=True)]
    for t in threads:
        t.start()
    deadline = t0 + watch
    while time.monotonic() < deadline:
        if firsts["box_awake"] is None and svc.is_awake() is True:
            firsts["box_awake"] = time.monotonic() - t0
        if firsts["box_awake"] is not None:
            hdmi = svc.hdmi_state()
            if hdmi.tv_power == "on" and firsts["tv_on"] is None:
                firsts["tv_on"] = time.monotonic() - t0
            if hdmi.active_source is True and firsts["active"] is None:
                firsts["active"] = time.monotonic() - t0
            if firsts["tv_on"] and firsts["active"] and firsts["tv_rest"]:
                break
        time.sleep(0.5)
    for t in threads:
        t.join(timeout=1)
    return firsts


def cmd_wake(svc: MediaService, args) -> int:
    rows = []
    for run in range(1, args.runs + 1):
        if run > 1 or args.sleep_first:
            print(f"[run {run}] turn_off() -> {svc.turn_off()}; waiting {args.between_minutes} min...")
            time.sleep(args.between_minutes * 60)
        print(f"[run {run}] room before: {_room(svc)}")
        if args.via_turn_on:
            t0 = time.monotonic()
            ok = svc.turn_on()
            total = time.monotonic() - t0
            hdmi = svc.hdmi_state()
            print(f"[run {run}] turn_on() -> {ok} in {total:.1f}s; last_wake_result={svc.last_wake_result}; "
                  f"tv_power={hdmi.tv_power} active_source={hdmi.active_source}")
            rows.append({"total": total})
        else:
            firsts = _time_primitives(svc, args.watch)
            print(f"[run {run}] " + ", ".join(f"{k}={v:.1f}s" if v is not None else f"{k}=None" for k, v in firsts.items()))
            rows.append(firsts)
    if rows:
        keys = rows[0].keys()
        print("Summary (mean / max over runs where measured):")
        for k in keys:
            vals = [r[k] for r in rows if r.get(k) is not None]
            if vals:
                print(f"  {k:>10}: {sum(vals)/len(vals):5.1f}s / {max(vals):5.1f}s  ({len(vals)}/{len(rows)} runs)")
            else:
                print(f"  {k:>10}: never")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the TV/box power path.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--debug", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("soak", help="sleep the room and watch how long the box stays reachable")
    p.add_argument("--minutes", type=float, default=60)
    p.add_argument("--interval", type=float, default=5)
    p.add_argument("--lost-after", type=int, default=3, help="consecutive closed polls that count as lost")

    p = sub.add_parser("otp", help="box asleep-but-reachable: send KEYCODE_WAKEUP only and watch the CEC tail")
    p.add_argument("--watch", type=float, default=40)

    p = sub.add_parser("keys", help="send the KEY_HDMI pair over the websocket and watch the CEC tail")
    p.add_argument("--presses", type=int, default=2)
    p.add_argument("--watch", type=float, default=30)

    p = sub.add_parser("wol-only", help="send Wake-on-LAN only and watch")
    p.add_argument("--watch", type=float, default=40)

    p = sub.add_parser("tv-standby", help="send one power key to the TV and read the CEC bus for the outcome")
    p.add_argument("--key", default="KEY_POWER", help="KEY_POWER (toggle, verified on the bus) or KEY_POWEROFF")
    p.add_argument("--watch", type=float, default=12)
    p.add_argument("--force", action="store_true", help="send even if the bus cannot confirm the TV is on")

    p = sub.add_parser("standby-mode",
                       help="full tv_only_standby round trip: TV off, box kept awake, then turn_on timed")
    p.add_argument("--hold", type=float, default=120, help="seconds to watch the box stay awake")
    p.add_argument("--interval", type=float, default=15)

    p = sub.add_parser("wake", help="time the wake, primitives in parallel or the real turn_on()")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--between-minutes", type=float, default=1)
    p.add_argument("--sleep-first", action="store_true", help="turn_off() before the first run too")
    p.add_argument("--via-turn-on", action="store_true")
    p.add_argument("--watch", type=float, default=45)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    config = yaml.safe_load(Path(args.config).read_text("utf-8"))
    svc = MediaService(config)
    if not svc.cec_waker.available:
        print(f"CEC wake is disabled: {svc.cec_waker.unavailable_reason}")
        return 1
    return {
        "soak": cmd_soak, "otp": cmd_otp, "keys": cmd_keys,
        "wol-only": cmd_wol_only, "wake": cmd_wake, "tv-standby": cmd_tv_standby,
        "standby-mode": cmd_standby_mode,
    }[args.cmd](svc, args)


if __name__ == "__main__":
    sys.exit(main())
