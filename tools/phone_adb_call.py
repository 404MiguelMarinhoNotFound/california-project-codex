"""
Phone calls over ADB, by hand: the same code California uses, without her.

    uv run python tools/phone_adb_call.py check
    uv run python tools/phone_adb_call.py state
    uv run python tools/phone_adb_call.py ring [--seconds 20]
    uv run python tools/phone_adb_call.py call <who|number> --kind personal --goal "..." [--details "..."]

`check`  finds the phone, pushes the callaudio helper if it changed, and
         reports the Security-settings gate and call-audio interception.
`state`  the phone's current call state.
`ring`   find my phone: the alarm-stream beep, through silent mode.
`call`   a real call through PhoneService (Gemini Live on config.yaml's phone
         backend). Running this command IS the confirmation, so there is no
         read-back. Test calls go to Master Miguel, never to other people.

Needs Wireless debugging paired with this laptop and HyperOS
"USB debugging (Security settings)" on; see android/callaudio/README.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_STATE_NAMES = {0: "idle", 1: "active", 2: "holding", 3: "dialing", 4: "alerting", 5: "incoming",
                6: "waiting", 7: "disconnected", 8: "disconnecting"}


def _config() -> dict:
    import yaml
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"))
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    config.setdefault("phone", {})["enabled"] = True
    return config


def _phone(config: dict):
    from services.phone_adb import AdbPhone

    adb = (config.get("phone", {}) or {}).get("adb", {}) or {}
    return AdbPhone(
        adb_path=str(adb.get("path") or "adb"), serial=str(adb.get("serial") or ""),
        port=int(adb.get("port") or 27183), mute_phone_mic=adb.get("mute_phone_mic", True) is not False,
    )


def cmd_check(config: dict) -> int:
    phone = _phone(config)
    status = phone.ready()
    print(f"phone: {phone.serial or 'not found'}")
    print(f"ready: {status}")
    if phone.serial:
        print(phone._run_helper("check").strip())
        print("call state:", _STATE_NAMES.get(phone.call_state(), "unknown"))
    return 0 if status == "OK" else 1


def cmd_state(config: dict) -> int:
    print(_STATE_NAMES.get(_phone(config).call_state(), "unknown"))
    return 0


def cmd_ring(config: dict, seconds: int) -> int:
    status = _phone(config).play_sound(seconds)
    print(status)
    return 0 if status == "PLAYING" else 1


def cmd_call(config: dict, who: str, kind: str, goal: str, details: str) -> int:
    from services.phone_prompts import CallBrief
    from services.phone_service import PhoneService, call_log_line, call_record
    from services.whatsapp_service import WhatsAppService

    service = PhoneService(config, resolve_contact=WhatsAppService(config).resolve_contact)
    if not service.enabled:
        print("phone calls are not set up (see the log line above)")
        return 1
    brief = CallBrief(to=who, kind=kind, goal=goal, details=details)
    first = service.call(brief)
    print(first.message)
    if not first.awaiting:
        return 1
    placed = service.call(brief, confirm=True)
    print(placed.message)
    if not placed:
        return 1
    service._thread.join()
    report = service.pop_report()
    print("\n" + call_log_line(call_record(report, include_transcript=False)))
    print(report.result.transcript(times=True))
    print("stats:", report.result.stats)
    return 0 if report.result.ended_by not in ("dial_failed", "error") else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("state")
    ring = sub.add_parser("ring")
    ring.add_argument("--seconds", type=int, default=20)
    call = sub.add_parser("call")
    call.add_argument("who")
    call.add_argument("--kind", default="general")
    call.add_argument("--goal", default="")
    call.add_argument("--details", default="")
    args = parser.parse_args()

    config = _config()
    if args.cmd == "check":
        return cmd_check(config)
    if args.cmd == "state":
        return cmd_state(config)
    if args.cmd == "ring":
        return cmd_ring(config, args.seconds)
    return cmd_call(config, args.who, args.kind, args.goal, args.details)


if __name__ == "__main__":
    raise SystemExit(main())
