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
    uv run python tools/bench_tv_power.py stremio --from-off           # dark room -> Stremio playing, phase by phase

Recorded runs, for docs/superpowers/specs/2026-10-02-tv-wake-optimization-design.md:

    uv run python tools/bench_tv_power.py wake --via-turn-on --from-deep --runs 3 --spike baseline   # S1
    uv run python tools/bench_tv_power.py stremio --from-deep --runs 3 --spike baseline              # S2-deep
    uv run python tools/bench_tv_power.py stremio --from-off --tv-only-standby --runs 3              # S2-shallow
    uv run python tools/bench_tv_power.py --set media.cec_wake.prewake_wol=true wake ...             # one spike
    uv run python tools/bench_tv_power.py standby-probe [--claim-first]                              # H10
    uv run python tools/bench_report.py bench/results/*.jsonl

Each recorded run appends one JSON line (phases in ms from the start) and
restores the box's HDMI-CEC settings in a `finally`, verified by reading back.

Never sends KEYCODE_POWER -- it is a toggle, see CLAUDE.md. `soak` and `wake`
put the room to sleep on purpose; run them when nobody is watching.
"""

import argparse
import copy
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import phase_marks  # noqa: E402
from services.device_finder import _ips_for_mac, _read_arp_table, port_open  # noqa: E402
from services.media_service import (  # noqa: E402
    MediaService,
    _parse_active_source,
    _parse_tv_power,
    _parse_wakefulness,
)

log = logging.getLogger("tools.bench_tv_power")

# The box settings a run may change, directly (tv_only_standby, claim_active_source)
# or through a crash in the middle of one. Captured before every recorded run and
# put back after it.
SETTING_KEYS = (
    "hdmi_control_enabled",
    "hdmi_control_one_touch_play_enabled",
    "hdmi_control_auto_device_off_enabled",
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


# --- recorded runs ----------------------------------------------------------


def apply_overrides(config: dict, sets: list[str]) -> dict:
    """`a.b.c=value` pairs deep-merged into a copy of config; values parsed as YAML."""
    merged = copy.deepcopy(config)
    for item in sets or []:
        dotted, _, raw = item.partition("=")
        node = merged
        *parents, leaf = dotted.strip().split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = yaml.safe_load(raw)
    return merged


def capture_settings(svc: MediaService) -> dict[str, str]:
    before = {}
    for key in SETTING_KEYS:
        ok, out = svc._adb(f"shell settings get global {key}")
        before[key] = (out or "").strip() if ok else ""
    return before


def restore_settings(svc: MediaService, before: dict[str, str]) -> bool:
    """Put back every captured setting that changed, then read all of them back."""
    ok_all = True
    for key, value in before.items():
        if value in ("", "null"):
            continue  # nothing was set, so there is nothing to put back
        ok, now = svc._adb(f"shell settings get global {key}")
        if ok and (now or "").strip() == value:
            continue
        if ok:
            svc._adb(f"shell settings put global {key} {value}")
        ok, now = svc._adb(f"shell settings get global {key}")
        if not ok or (now or "").strip() != value:
            log.warning("Could not restore %s to %s (box says %r) -- set it by hand",
                        key, value, (now or "").strip() if ok else "unreachable")
            ok_all = False
    return ok_all


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=False, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _tv_ip(svc: MediaService) -> str:
    waker = svc.cec_waker
    return (waker.resolve_tv_ip(discover=False) or "") if waker.available else ""


class PortObserver(threading.Thread):
    """
    The outside half of the timeline: first time each port answers, in ms from t0.

    Plain TCP connects only -- never adb -- so watching cannot change what the
    code under test sees. 9197 is the TV's UPnP, which only answers while it is
    powered; 8001 also answers in shallow standby; 5555 is the box on the LAN.
    """

    def __init__(self, tv_ip: str, box_ip: str, t0: float, interval_s: float = 0.5):
        super().__init__(daemon=True)
        self.targets = [(name, ip, port) for name, ip, port in (
            ("tv_8001_up", tv_ip, 8001), ("tv_9197_up", tv_ip, 9197), ("box_on_lan", box_ip, 5555),
        ) if ip]
        self.t0 = t0
        self.interval_s = interval_s
        self.phases: dict[str, float] = {}
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            for name, ip, port in self.targets:
                if name not in self.phases and port_open(ip, port, 0.3):
                    self.phases[name] = (time.monotonic() - self.t0) * 1000
            self._stop_event.wait(self.interval_s)

    def stop(self):
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=2)


def run_recorded(svc: MediaService, scenario: str, spike: str, run: int,
                 action: Callable[[], object], out: Path) -> dict:
    """
    Time one action and append its record to `out`. Settings are captured before
    and restored in a finally -- on failure, on an exception, on Ctrl-C -- and
    the record says whether the restore verified.
    """
    before = capture_settings(svc)
    record = {"scenario": scenario, "spike": spike, "git_sha": _git_sha(), "run": run,
              "phases": {}, "total_ms": None, "outcome": "error",
              "settings_before": before, "settings_restored": False}
    t0 = time.monotonic()
    observer = PortObserver(_tv_ip(svc), svc.ip, t0)
    observer.start()
    try:
        with phase_marks.recording() as marks:
            try:
                result = action()
                record["total_ms"] = (time.monotonic() - t0) * 1000
                record["outcome"] = "ok" if result else "failed"
            except KeyboardInterrupt:
                record["outcome"] = "interrupted"
                raise
            finally:
                record["phases"].update(marks)
    finally:
        observer.stop()
        record["phases"].update(observer.phases)
        record["settings_restored"] = restore_settings(svc, before)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        total = record["total_ms"]
        print(f"[{scenario} {spike} run {run}] outcome={record['outcome']} "
              f"total={'-' if total is None else f'{total / 1000:.1f}s'} "
              f"settings_restored={record['settings_restored']}", flush=True)
    return record


def put_room_in_deep_standby(svc: MediaService, idle_s: float) -> bool:
    """
    S1's start state, unattended: what the Samsung remote's power button does.

    KEY_POWER to the TV with the box's HDMI-CEC LEFT ON, so the set's <Standby>
    broadcast sleeps the box and it force-suspends ~15s later. KEY_POWER is a
    toggle, so it goes only behind a fresh "on" reading from the bus -- the
    same rule as CecWaker.standby_tv. Then wait for 5555 to close and idle.
    """
    if svc.is_awake() is not True:
        print("Deep-standby start: the box must be awake first.")
        return False
    if svc.hdmi_state().tv_power != "on":
        print("Deep-standby start: the bus does not say the TV is on, so a toggle could turn it ON.")
        return False
    svc._finish_pending_standby()
    # _send_keys builds its websocket from the resolved address; without this
    # it fails as "Can't build URL with port but without host".
    if not svc.cec_waker.resolve_tv_ip():
        print("Deep-standby start: the TV is not reachable over REST, so no key can go.")
        return False
    if not svc.cec_waker._send_keys(["KEY_POWER"]):
        print("Deep-standby start: the TV did not take KEY_POWER.")
        return False
    deadline = time.monotonic() + 120
    while port_open(svc.ip, svc.port, 0.3):
        if time.monotonic() >= deadline:
            print("Deep-standby start: the box was still on the LAN after 120s.")
            return False
        time.sleep(2)
    print(f"Box off the LAN; idling {idle_s:.0f}s before the clock starts.", flush=True)
    time.sleep(idle_s)
    return True


def _results_path(args, scenario: str) -> Path:
    if args.json:
        return Path(args.json)
    return Path("bench/results") / f"{date.today().isoformat()}-{scenario}.jsonl"


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
    the television to sleep with the box's HDMI-CEC off so it never hears the
    set's <Standby>, the box stays awake for `--hold` seconds, and then
    turn_on() is timed.

    Forces media.power.tv_only_standby on for this run so the shipped config
    does not have to be edited to measure it.
    """
    print("Room:", _room(svc))
    if svc.is_awake() is not True:
        print("Precondition: the box must be awake and reachable.")
        return 2
    before = svc.hdmi_state()
    tv = svc.cec_waker.tv_power()
    if tv != "on":
        print(f"The TV does not read as on (tv_power={tv}); turn_off would refuse "
              f"to send a toggle. Turn the TV on first.")
        return 2

    svc.tv_only_standby = True
    print(f"hdmi_control_enabled before: {svc._hdmi_control_setting()}")

    t0 = time.monotonic()
    ok = svc.turn_off()
    print(f"  {_stamp(t0)} turn_off() -> {ok}; last={svc.last_wake_result}", flush=True)
    print(f"  {_stamp(t0)} hdmi_control_enabled after: {svc._hdmi_control_setting()}")

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
    print(f"final: tv_power={svc.cec_waker.tv_power()} active_source={hdmi.active_source} "
          f"hdmi_control_enabled={svc._hdmi_control_setting()}")
    return 0


def _bring_room_up(svc: MediaService) -> bool:
    """Untimed: the box awake and the TV on, so a start state can be set up."""
    if svc.is_awake() is True and svc.hdmi_state().tv_power == "on":
        return True
    print("Bringing the room up first (untimed)...", flush=True)
    return bool(svc.turn_on()) and svc.is_awake() is True


def _start_clean(svc: MediaService) -> None:
    """Forget the bench's own recent failures, as an idle orchestrator would have."""
    svc._last_fail_time = 0
    svc._last_discovery_t = 0


def _prepare_start(svc: MediaService, args) -> bool:
    if args.from_deep:
        return _bring_room_up(svc) and put_room_in_deep_standby(svc, args.idle)
    if getattr(args, "from_off", False):
        if not _bring_room_up(svc):
            return False
        svc.stop()  # untimed: nothing left playing behind a dark set
        svc.go_home()
        print(f"Putting the room away (tv_only_standby={svc.tv_only_standby})...", flush=True)
        print("  turn_off() ->", svc.turn_off(), flush=True)
        print(f"  waiting {args.settle:.0f}s so the room is properly away...", flush=True)
        time.sleep(args.settle)
    return True


def _stremio_once(svc: MediaService, stremio, args) -> bool:
    from core.orchestrator import _dispatch_tv, _ensure_playable  # noqa: PLC0415

    print("Room at the start:", _room(svc), flush=True)
    t0 = time.monotonic()
    spoken = []

    line = _ensure_playable(svc, say_now=spoken.append)
    t_wake = time.monotonic() - t0
    print(f"  {_stamp(t0)} _ensure_playable -> {line!r}", flush=True)
    if spoken:
        print(f"  (she would have said: {spoken[0]!r} at ~0.0s)", flush=True)
    if line:
        print(f"RESULT: the room never became playable after {t_wake:.1f}s.")
        return False

    reply = _dispatch_tv({"action": "stremio_play", "title": args.title}, svc,
                         stremio, None, {}, None, None)
    t_launch = time.monotonic() - t0
    print(f"  {_stamp(t0)} stremio_play -> {reply!r}", flush=True)

    # Playing is what the box says, never what the launch returned.
    playing_at = None
    deadline = time.monotonic() + args.watch
    while time.monotonic() < deadline:
        status = svc.room_status()
        if getattr(status, "playing", None) is True:
            playing_at = time.monotonic() - t0
            print(f"  {_stamp(t0)} box reports playing: {getattr(status, 'title', None)!r}", flush=True)
            break
        time.sleep(1)

    print(f"  wake the room      {t_wake:6.1f}s")
    print(f"  stremio launch     {t_launch - t_wake:6.1f}s  (cumulative {t_launch:.1f}s)")
    if playing_at is None:
        print(f"  playing            never within {args.watch:.0f}s of the start")
        return False
    print(f"  TOTAL to playing   {playing_at:6.1f}s")
    return True


def cmd_stremio(svc: MediaService, args) -> int:
    """
    The number that actually matters: "put on <title>" with the room off, to
    something playing on screen.

    Runs the real dispatch path -- _ensure_playable (which wakes the room) then
    the stremio_play branch -- and times each phase, so a slow answer can be
    blamed on the wake, on Stremio's own launch, or on the stream buffering,
    rather than guessed at.

    --from-deep starts each run where the Samsung remote leaves the room (box
    suspended); --from-off where California's own turn_off does.
    """
    from services.stremio_service import StremioService  # noqa: PLC0415

    # The merged config, so --set reaches StremioService as well as MediaService.
    stremio = StremioService(args.config_dict, media_service=svc)
    svc.tv_only_standby = bool(args.tv_only_standby)
    scenario = "S2-deep" if args.from_deep else ("S2-shallow" if args.from_off else "S2")
    out = _results_path(args, scenario)
    for run in range(1, args.runs + 1):
        if not _prepare_start(svc, args):
            print("Could not set up the start state; stopping the series.")
            return 1
        _start_clean(svc)
        record = run_recorded(svc, scenario, args.spike, run,
                              lambda: _stremio_once(svc, stremio, args), out)
        if record["outcome"] != "ok" and args.from_deep and svc.is_awake() is None:
            print("STOP: the box is not back after a full wake attempt. Not retrying.")
            return 1
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
    if args.via_turn_on:
        return _wake_recorded(svc, args)
    rows = []
    for run in range(1, args.runs + 1):
        if run > 1 or args.sleep_first:
            print(f"[run {run}] turn_off() -> {svc.turn_off()}; waiting {args.between_minutes} min...")
            time.sleep(args.between_minutes * 60)
        print(f"[run {run}] room before: {_room(svc)}")
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


def _wake_recorded(svc: MediaService, args) -> int:
    """The real turn_on(), recorded. --from-deep is scenario S1."""
    scenario = "S1" if args.from_deep else "wake"
    out = _results_path(args, scenario)
    for run in range(1, args.runs + 1):
        if args.from_deep:
            if not _prepare_start(svc, args):
                print("Could not set up the start state; stopping the series.")
                return 1
        elif run > 1 or args.sleep_first:
            print(f"[run {run}] turn_off() -> {svc.turn_off()}; waiting {args.between_minutes} min...")
            time.sleep(args.between_minutes * 60)
        print(f"[run {run}] room before: {_room(svc)}")
        _start_clean(svc)
        record = run_recorded(svc, scenario, args.spike, run, svc.turn_on, out)
        print(f"[run {run}] last_wake_result={svc.last_wake_result}")
        if record["outcome"] != "ok" and args.from_deep:
            print("STOP: turn_on() did not bring the box back. Not retrying.")
            return 1
    return 0


def cmd_standby_probe(svc: MediaService, args) -> int:
    """
    H10: does the box survive the Samsung remote's power button?

    In AOSP 11, HdmiControlService.standby() refuses while the playback device
    holds its keep-awake wakelock, which it takes while it is the active source
    and persist.sys.hdmi.keep_awake is true. Read both, optionally claim the
    active source first, then have a person press the remote and watch.
    """
    if svc.is_awake() is not True:
        print("Precondition: the box must be awake, with the TV on.")
        return 2
    ok, keep = svc._adb("shell getprop persist.sys.hdmi.keep_awake")
    print(f"persist.sys.hdmi.keep_awake = {(keep or '').strip() if ok else 'unreadable'!r}")
    print(f"active_source = {svc.hdmi_state().active_source}")
    if args.claim_first:
        print(f"claim_active_source() -> {svc.claim_active_source()}")
        time.sleep(2)
        print(f"active_source now = {svc.hdmi_state().active_source}")
    if args.watch <= 1:
        return 0
    print("Press the Samsung remote's power button now.", flush=True)
    t0 = time.monotonic()
    slept = False
    while time.monotonic() - t0 < args.watch:
        awake = svc.is_awake()
        print(f"  {_stamp(t0)} box awake={awake}", flush=True)
        if awake is not True:
            slept = True
        time.sleep(1)
    print("Verdict:", "box slept" if slept else "box stayed awake")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the TV/box power path.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--set", action="append", default=[], metavar="dotted.key=value",
                        help="override a config value for this run (repeatable, YAML-parsed)")
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

    p = sub.add_parser("stremio",
                       help="TIME TO PLAYING: 'put on <title>' from a dark room, phase by phase")
    p.add_argument("--title", default="Fallout")
    p.add_argument("--from-off", action="store_true",
                   help="put the room away first, so the clock starts at a dark television")
    p.add_argument("--tv-only-standby", action="store_true",
                   help="use the TV-only standby for --from-off (box stays awake)")
    p.add_argument("--settle", type=float, default=30,
                   help="seconds to wait after turn_off before starting the clock")
    p.add_argument("--watch", type=float, default=180,
                   help="how long to wait for the box to report playing")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--json", default=None, help="results file (default bench/results/<date>-<scenario>.jsonl)")
    p.add_argument("--spike", default="baseline", help="label for this configuration")
    p.add_argument("--from-deep", action="store_true",
                   help="start each run from deep standby (Samsung-remote style), box off the LAN")
    p.add_argument("--idle", type=float, default=120, help="seconds idle in deep standby before the clock")

    p = sub.add_parser("standby-mode",
                       help="full tv_only_standby round trip: TV off, box kept awake, then turn_on timed")
    p.add_argument("--hold", type=float, default=120, help="seconds to watch the box stay awake")
    p.add_argument("--interval", type=float, default=15)

    p = sub.add_parser("wake", help="time the wake, primitives in parallel or the real turn_on()")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--json", default=None, help="results file (default bench/results/<date>-<scenario>.jsonl)")
    p.add_argument("--spike", default="baseline", help="label for this configuration")
    p.add_argument("--from-deep", action="store_true",
                   help="start each run from deep standby (Samsung-remote style), box off the LAN")
    p.add_argument("--idle", type=float, default=120, help="seconds idle in deep standby before the clock")

    p.add_argument("--between-minutes", type=float, default=1)
    p.add_argument("--sleep-first", action="store_true", help="turn_off() before the first run too")
    p.add_argument("--via-turn-on", action="store_true")
    p.add_argument("--watch", type=float, default=45)

    p = sub.add_parser("standby-probe", help="H10: does the box survive the Samsung remote's power button?")
    p.add_argument("--claim-first", action="store_true", help="have the box claim the active source first")
    p.add_argument("--watch", type=float, default=30)

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    # Stremio and TMDB read their credentials from the environment, the same
    # as main.py; without this the launch fails as "I couldn't find it".
    load_dotenv()
    config = apply_overrides(yaml.safe_load(Path(args.config).read_text("utf-8")), args.set)
    args.config_dict = config
    svc = MediaService(config)
    if not svc.cec_waker.available:
        print(f"CEC wake is disabled: {svc.cec_waker.unavailable_reason}")
        return 1
    return {
        "soak": cmd_soak, "otp": cmd_otp, "keys": cmd_keys,
        "wol-only": cmd_wol_only, "wake": cmd_wake, "tv-standby": cmd_tv_standby,
        "standby-mode": cmd_standby_mode, "stremio": cmd_stremio,
        "standby-probe": cmd_standby_probe,
    }[args.cmd](svc, args)


if __name__ == "__main__":
    sys.exit(main())
