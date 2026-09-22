"""
Orchestrator — The brain of Project California.

State machine that coordinates all components:
  IDLE → (wake word) → LISTENING → (silence) → PROCESSING → SPEAKING → IDLE

The key insight: the PROCESSING → SPEAKING transition is STREAMED.
LLM tokens flow through sentence chunker into TTS, so the user hears
the first sentence while the LLM is still generating.
"""

import collections
import glob
import os
import random
import time
import logging
import threading
import queue
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.activation_phrases import EchoGate, resolve_tier, strip_activation_echo
from services.deebot_service import DeebotService
from services.govee_service import GoveeService, clamp_percent, resolve_color
from services.media_service import MediaService
from services.light_shadow import LightShadow
from services.now_playing import NowPlaying
from services.stremio_service import AUTOPLAY_FALLBACK_LINE, StremioService
from services.surfshark_service import SurfsharkService
from services.youtube_playlist_resolver import resolve_playlist_choice
from services.youtube_search import speakable_title, top_video

logger = logging.getLogger(__name__)

_LIGHTS_UNREACHABLE = "I couldn't reach your lights just now."
_VACUUM_UNREACHABLE = "I couldn't reach the vacuum just now."


# Packages that more than one boot thread imports for the first time. Importing
# them here, serially, before the pool starts is what keeps the pool safe.
#
# Python holds one lock per module while it imports. Two threads importing
# overlapping graphs at the same instant can each end up waiting on a lock the
# other holds, and 3.14 detects that cycle and raises _DeadlockError instead of
# hanging. Seen live on Windows: the wake-word thread (openwakeword -> tqdm ->
# colorama) and the TTS thread (Kokoro -> huggingface_hub -> tqdm -> colorama)
# collided on colorama.win32, and boot failed with "Component init failed:
# wake_word". It is timing-dependent, so it does not happen every run.
#
# Nothing is lost by doing this first: an import is serialised by that lock
# anyway, so the second thread would only have waited on the first.
_SHARED_BOOT_IMPORTS = ("tqdm", "torch", "huggingface_hub")


def _warm_shared_imports() -> None:
    import importlib
    for name in _SHARED_BOOT_IMPORTS:
        try:
            importlib.import_module(name)
        except ImportError:
            # Optional extras (torch only ships with the silero/kokoro extras).
            pass


def _build_parallel(tasks: dict) -> dict:
    """Run each zero-arg callable in `tasks` on its own thread and return
    {name: result}. Every task runs to completion before this raises, so one
    slow or failing component never hides how the others did. On failure,
    raises RuntimeError naming every failed task, chained from the first
    exception.

    Boot-time component construction is dominated by model loads (Kokoro TTS,
    Silero VAD, the wake-word ONNX model) and network calls (ADB connect and
    device discovery, Stremio login + library sync) that don't depend on each
    other's results — they were simply written one after another. Threads are
    enough here even though CPython has a GIL: model loading is disk I/O and
    the compute-heavy parts (torch, onnxruntime) release the GIL, and the
    network calls are pure I/O wait.
    """
    results: dict = {}
    errors: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, len(tasks)), thread_name_prefix="boot") as pool:
        future_to_name = {pool.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - re-raised below with context
                errors[name] = exc

    if errors:
        failed = ", ".join(errors)
        first_exc = next(iter(errors.values()))
        raise RuntimeError(f"Component init failed: {failed}") from first_exc

    return results


ROUTED_ACTIONS = {
    "youtube_playlist": ("youtube", "restart_autoconnect"),
    "youtube_search": ("youtube", "restart_autoconnect"),
    "stremio_play": ("stremio", "quick_connect"),
    "stremio_continue": ("stremio", "quick_connect"),
}


def _route_target_for_action(
    action: str,
    params: dict,
    route_by_app: dict | None = None,
) -> tuple[str | None, str | None]:
    route_by_app = route_by_app or {
        "youtube": "restart_autoconnect",
        "stremio": "quick_connect",
    }
    if action == "launch_app":
        app = (params.get("app_name") or "").strip().lower()
        country = route_by_app.get(app)
        if country:
            return app, country
        return None, None
    target = ROUTED_ACTIONS.get(action, (None, None))
    if target[0]:
        return target[0], route_by_app.get(target[0], target[1])
    return target


def _vpn_warning_suffix(target_route: str | None) -> str:
    normalized = (target_route or "").strip().lower()
    if normalized == "restart_autoconnect":
        return " but I couldn't complete Surfshark Albania auto-connect."
    if normalized in {"quick_connect", "quick connect", "fastest", "fastest location"}:
        return " but I couldn't complete Surfshark Quick Connect."
    route_label = (target_route or "the required route").replace("_", " ").title()
    return f" but I couldn't complete {route_label}."


def _append_route_warning(message: str, warning_suffix: str | None) -> str:
    if not warning_suffix:
        return message
    base = (message or "").rstrip()
    if base.endswith("."):
        base = base[:-1]
    return base + warning_suffix


def _unreachable_line(media_svc) -> str:
    """
    Say WHICH kind of unreachable. These need opposite responses from him.

    "The box is off" and "the box is fine but ADB over Wi-Fi got turned off by a
    reboot" used to produce the identical sentence, and only one of them is fixed
    by waiting or retrying.
    """
    reason = getattr(media_svc, "unreachable_reason", "") or ""
    return {
        "cooldown": "The box was unreachable a moment ago, give me a few seconds.",
        "not_on_lan": "The box is off, I can't see it on the network at all.",
        "no_adb_port": (
            "I can see the box but it isn't accepting commands. ADB over Wi-Fi "
            "needs turning back on in developer options."
        ),
        "identity_mismatch": "Something else has the box's address, I can't find the box itself.",
    }.get(reason, "TV is off or unreachable right now")


def _human_position(seconds) -> str | None:
    """
    How far into it he is, in words. None when the box did not say.

    There is no duration to measure against -- `dumpsys media_session` prints
    the description and no length -- so this is elapsed time, never a
    percentage and never "time left".
    """
    if seconds is None or seconds < 0:
        return None
    minutes, hours = seconds // 60, seconds // 3600
    if minutes < 1:
        return "just started"
    if hours < 1:
        return f"{minutes} minute{'' if minutes == 1 else 's'} in"
    minutes -= hours * 60
    if minutes == 0:
        return f"{hours} hour{'' if hours == 1 else 's'} in"
    return f"{hours}h{minutes:02d} in"


def _remember_launch(now_playing, app: str, label: str, kind: str = "playing") -> None:
    """
    Record what she just put on. A None store is a no-op, which is what the
    11 existing _dispatch_tv tests pass and what keeps them honest: no memory
    means she admits she does not know, rather than a shared global leaking a
    label from one test into the next.
    """
    if now_playing is not None:
        now_playing.remember(app, label, kind)


def _forget_launch(now_playing) -> None:
    if now_playing is not None:
        now_playing.forget()


def _status_line(status, launch=None) -> str:
    """
    Turn a RoomStatus into something speakable.

    Data lives in MediaService, speech lives here -- the same split as
    unreachable_reason / _unreachable_line.

    The rule that matters: **a field we could not read is a clause we do not
    say.** None means "could not tell", never "no", and the old version of this
    printed "unknown" for exactly the fields it had failed to read. Three
    readable facts make a shorter answer, not a worse one.
    """
    if not status.reachable:
        return "I can't reach the box right now."

    parts = []

    if status.tv_power == "standby":
        parts.append("TV off")
    elif status.tv_power == "on":
        if status.on_the_box is True:
            parts.append("TV on and showing the box")
        elif status.on_the_box is False:
            parts.append("TV on but showing another input")
        else:
            parts.append("TV on")
    elif status.on_the_box is False:
        # Power unreadable, but "he is on another input" still answers the
        # question he actually asked.
        parts.append("TV showing another input")

    if status.awake is False:
        # Asleep means nothing is on screen, so app and playback were never read.
        parts.append("box asleep")
        return ". ".join(parts) + "."

    # What the box itself reports wins. Stremio publishes the show AND the
    # episode in its session metadata ("Fallout, The Strip"), and unlike the
    # launch memory that survives him starting something with the remote.
    #
    # The memory is the fallback, and only a launch she made HERSELF and
    # actually played may name anything: a search or a series page was opened,
    # not played, and calling that playing is the same class of lie as a
    # standby television read as on.
    named = status.title or (
        launch.label if launch is not None and launch.kind == "playing" else None
    )

    clause = None
    if status.app and status.playing is True:
        clause = (
            f"{status.app} playing {named}" if named
            else f"{status.app} playing, and I didn't start it so I can't say what"
        )
    elif status.app and status.playing is False:
        # Stremio keeps its metadata across a pause, so it still knows what is
        # loaded. Saying "nothing playing" and dropping the title throws away
        # the more useful half of what was read. The word comes from the
        # session state, so paused / stopped / buffering are never guessed at.
        if named and status.playback:
            clause = f"{status.app} {status.playback} on {named}"
        else:
            clause = f"{status.app} open, nothing playing"
    elif status.app:
        clause = f"{status.app} open"
    elif status.playing is True:
        clause = "something playing"

    if clause:
        # Elapsed time only belongs on a clause that named something.
        where = _human_position(status.position_s) if named else None
        parts.append(f"{clause}, {where}" if where else clause)

    # The box's own volume, which is the one volume_set moves. NOT the
    # television's -- that lives on the Samsung and ADB cannot see it.
    if status.muted:
        parts.append("box muted")
    elif status.volume is not None and status.volume_max:
        parts.append(f"box volume {status.volume} of {status.volume_max}")

    if status.awake is True:
        parts.append("box awake")

    if not parts:
        return "I can't tell what the box is doing right now."
    return ". ".join(parts) + "."


def _ensure_playable(media_svc, say_now=None) -> str:
    """
    Get the room ready to show something. Returns "" on success, else a spoken line.

    The old gate was binary: if ensure_connected() failed it said "TV is off or
    unreachable right now" and stopped, so "put on Fallout" with the room asleep
    simply did not work -- recovery depended on the model deciding to call turn_on
    and re-issue the action, two extra round trips. One tool call should produce
    one outcome.

    Reachable is NOT "ready to show something", and that used to be assumed:

      awake, TV on/unknown -> only check the input has not been parked elsewhere.
      awake, TV in standby -> the television is dark under a running box (the
                              tv_only_standby mode leaves it exactly so): turn_on
                              does the TV half only.
      asleep but reachable -> a deep link fired now lands on a black screen;
                              turn_on wakes both halves in parallel (~10-15s).
      unreachable          -> ensure_connected() has already tried rediscovery,
                              so the address is not the problem: wake through
                              the television over CEC (~52s).

    `say_now` speaks an interim line without ending the turn. A wake takes 10s
    to a minute and a tool call that blocks that long in silence reads as a hang.
    """
    if not media_svc:
        return "media service not available"

    remote_line = ("The box is awake but I can't get the TV onto its input. "
                   "Switch to it with the remote and I'll take it from there.")

    # Identity checks throughout: a bare Mock's attributes are truthy.
    if media_svc.is_awake() is True:
        hdmi = media_svc.hdmi_state()
        if getattr(hdmi, "tv_power", None) != "standby":
            # Verified, not fire-and-forget: switch_input reports success on a
            # key the TV accepted and ignored. None is "could not tell" and never
            # switches; it fails open, as it always has.
            if media_svc.ensure_active_source() is False:
                return remote_line
            return ""

    if say_now:
        say_now("Hold on, waking everything up.")
    logger.info("Room not ready for a playback action; attempting a wake")
    if media_svc.turn_on() and media_svc.ensure_connected():
        result = getattr(media_svc, "last_wake_result", None)
        if result is not None and getattr(result, "needs_pairing", False) is True:
            return ("The box is on, but the TV rejected my pairing token. Approve "
                    "me on screen and I'll try again.")
        if getattr(result, "tv_confirmed", None) is False:
            return remote_line
        # True, or None (unconfirmed): proceed. A deep link into a room that
        # is probably fine beats refusing to play on a maybe.
        return ""

    result = getattr(media_svc, "last_wake_result", None)
    if result is not None and getattr(result, "needs_pairing", False) is True:
        return ("The TV rejected my pairing token. Approve me on screen and I'll "
                "try again.")
    return _unreachable_line(media_svc)


def _dispatch_tv(
    params: dict,
    media_svc,
    stremio_svc,
    surfshark_svc,
    youtube_playlists: dict,
    youtube_playlist_aliases: dict | None = None,
    say_now=None,
    now_playing=None,
) -> str:
    action = params.get("action")
    route_warning = None

    requires_tv = {
        "play_pause", "stop", "next", "prev", "fast_forward", "rewind",
        "volume_up", "volume_down", "volume_set", "mute",
        "launch_app", "go_home", "go_back",
        # Deliberately NOT the power actions. This gate returns early when the
        # TV is unreachable, and unreachable is precisely the state turn_on
        # exists to fix -- the box drops off Wi-Fi in standby. Listing "wake"
        # here is part of why it was dead code: the gate answered first.
        "get_status", "youtube_playlist", "youtube_search",
        "stremio_play", "stremio_continue",
    }

    # Actions that put something on the screen escalate instead of giving up: a
    # dark room is a fixable state, not an error.
    needs_screen = {
        "launch_app", "youtube_playlist", "youtube_search",
        "stremio_play", "stremio_continue",
    }

    if action in needs_screen:
        problem = _ensure_playable(media_svc, say_now)
        if problem:
            return problem
    elif action in requires_tv:
        # Everything else -- transport controls, volume, status -- is meaningless
        # against a sleeping box and is not worth a 25s wake unasked.
        if not media_svc:
            return "media service not available"
        if not media_svc.ensure_connected():
            return _unreachable_line(media_svc)

    routing_enabled = bool(getattr(surfshark_svc, "enabled", False)) if surfshark_svc else False
    route_by_app = getattr(surfshark_svc, "route_by_app", None) if routing_enabled else None
    target_app, target_route = _route_target_for_action(action, params, route_by_app)
    if target_app and routing_enabled:
        is_foreground = media_svc.is_app_foreground(target_app)
        logger.info(
            "VPN preflight for action=%s target_app=%s target_route=%s already_foreground=%s",
            action,
            target_app,
            target_route,
            is_foreground,
        )
        if not is_foreground:
            t_vpn = time.monotonic()
            vpn_result = surfshark_svc.ensure_route(target_route)
            logger.info(
                "[timing] VPN preflight %s took %.3fs success=%s",
                target_route, time.monotonic() - t_vpn, vpn_result.success,
            )
            logger.info(
                "VPN preflight result for %s: success=%s switched=%s current_country=%s message=%s",
                target_app,
                vpn_result.success,
                vpn_result.switched,
                vpn_result.current_country,
                vpn_result.message,
            )
            if not vpn_result.success:
                route_warning = _vpn_warning_suffix(target_route)
            if target_app == "youtube":
                t_fstop = time.monotonic()
                stopped = media_svc.force_stop_app("youtube")
                logger.info("[timing] Post-VPN YouTube force-stop took %.3fs ok=%s", time.monotonic() - t_fstop, stopped)
        else:
            logger.info("Skipping VPN preflight because %s is already foreground", target_app)

    # Playback
    if action == "play_pause":
        return "done" if media_svc.play_pause() else "command failed"
    elif action == "stop":
        _forget_launch(now_playing)
        return "done" if media_svc.stop() else "command failed"
    elif action == "next":
        media_svc.next_track()
        return "done"
    elif action == "prev":
        media_svc.prev_track()
        return "done"
    elif action == "fast_forward":
        media_svc.fast_forward()
        return "done"
    elif action == "rewind":
        media_svc.rewind()
        return "done"

    # Volume
    elif action == "volume_up":
        media_svc.volume_up(params.get("volume_steps", 10))
        return "done"
    elif action == "volume_down":
        media_svc.volume_down(params.get("volume_steps", 10))
        return "done"
    elif action == "volume_set":
        pct = params.get("volume_percent", 50)
        media_svc.volume_set(pct)
        return f"volume set to roughly {pct}%"
    elif action == "mute":
        media_svc.mute()
        return "muted"

    # App launching
    elif action == "launch_app":
        ok, msg = media_svc.launch_app(params.get("app_name", ""))
        if ok:
            # A bare app launch puts nothing specific on, so whatever she
            # remembers is now wrong.
            _forget_launch(now_playing)
        return _append_route_warning(msg, route_warning) if ok else msg

    # Stremio
    elif action == "stremio_sync_library":
        if not stremio_svc:
            return "stremio service not available"
        try:
            synced = stremio_svc.sync_library()
            return "Stremio library synced." if synced else "I couldn't sync your Stremio library right now."
        except Exception as exc:
            logger.warning("Stremio sync failed: %s", exc)
            return "I couldn't sync your Stremio library right now."

    elif action == "stremio_get_progress":
        if not stremio_svc:
            return "stremio service not available"
        title = (params.get("title") or "").strip()
        if not title:
            return "Tell me the series name and I'll check the episode."
        entry = stremio_svc.get_progress(title, refresh_if_stale=True)
        if not entry:
            return f"I couldn't find {title} in your Stremio watch state yet."
        if entry.get("type") == "series":
            season = entry.get("season")
            episode = entry.get("episode")
            if season and episode:
                return f"You're on season {season} episode {episode} of {entry.get('title', title)}."
            return f"I found {entry.get('title', title)}, but episode progress isn't available yet."
        return f"{entry.get('title', title)} is tracked as a movie in your library."

    elif action == "stremio_continue":
        if not stremio_svc:
            return "stremio service not available"
        title = (params.get("title") or "").strip()
        if not title:
            return "Tell me what show you want to continue."
        try:
            result = stremio_svc.play(
                title=title,
                media_type="series",
                allow_unknown_source=bool(params.get("allow_unknown_source", False)),
            )
        except Exception as exc:
            logger.warning("Stremio continue failed: %s", exc)
            return f"I couldn't find {title} in Stremio or TMDB."
        if result.requires_confirmation:
            return result.message or AUTOPLAY_FALLBACK_LINE
        if result.success:
            if result.target_mode == "episode":
                response = f"Continuing {title}."
            else:
                response = f"Opening {title} on Stremio."
            _remember_launch(
                now_playing, "stremio", title,
                "playing" if result.target_mode == "episode" else "opened",
            )
            return _append_route_warning(response, route_warning)
        return result.message or AUTOPLAY_FALLBACK_LINE

    elif action == "stremio_play":
        if not stremio_svc:
            return "stremio service not available"
        title = (params.get("title") or "").strip()
        if not title:
            return "Tell me what you want to play on Stremio."
        try:
            result = stremio_svc.play(
                title=title,
                media_type=params.get("media_type"),
                season=params.get("season"),
                episode=params.get("episode"),
                allow_unknown_source=bool(params.get("allow_unknown_source", False)),
            )
        except Exception as exc:
            logger.warning("Stremio play failed: %s", exc)
            return f"I couldn't find {title} in Stremio or TMDB."

        if result.requires_confirmation:
            return result.message or AUTOPLAY_FALLBACK_LINE
        if result.success:
            response = f"Opening {title} on Stremio."
            _remember_launch(
                now_playing, "stremio", title,
                "playing" if result.target_mode == "episode" else "opened",
            )
            return _append_route_warning(response, route_warning)
        return result.message or AUTOPLAY_FALLBACK_LINE

    # YouTube
    elif action == "youtube_playlist":
        t_yt = time.monotonic()
        playlist_id = (params.get("playlist_id") or "").strip()
        playlist_name = (params.get("playlist_name") or "").strip()
        matched_key = None

        if not playlist_id:
            matched_key, playlist_id = resolve_playlist_choice(
                playlist_name, youtube_playlists, youtube_playlist_aliases
            )

        if not playlist_id:
            fallback_name = playlist_name or "that"
            return f"I don't have a {fallback_name} playlist saved. Want me to search YouTube for it?"

        ok = media_svc.youtube_playlist(playlist_id)
        logger.info("[timing] youtube_playlist dispatch took %.3fs ok=%s", time.monotonic() - t_yt, ok)
        if not ok:
            return "I couldn't open that YouTube playlist right now."
        if matched_key:
            _remember_launch(now_playing, "youtube", f"your {matched_key} playlist")
            response = f"Opening your {matched_key} playlist on YouTube."
            return _append_route_warning(response, route_warning)
        # An opaque PL... id has no speakable name, so remember nothing rather
        # than reading an id out loud later.
        _forget_launch(now_playing)
        response = "Opening that YouTube playlist."
        return _append_route_warning(response, route_warning)

    elif action == "youtube_search":
        t_yt = time.monotonic()
        query = (params.get("query") or "").strip()
        if not query:
            return "Tell me what to search for on YouTube."
        # "Search for X" means "and play it". The results deep link stops at
        # the results page (reported 2026-09-14: searched correctly, never
        # played), so resolve the query to the first video and play THAT.
        # The results page is only the fallback for a resolve that failed.
        video = None
        if getattr(media_svc, "youtube_search_autoplay", False):
            t_resolve = time.monotonic()
            video = top_video(query, timeout_s=media_svc.youtube_search_resolve_timeout_s)
            logger.info(
                "[timing] youtube_search resolve took %.3fs video=%s",
                time.monotonic() - t_resolve, video.video_id if video else None,
            )

        if video is None:
            ok = media_svc.youtube_search(query)
            logger.info("[timing] youtube_search dispatch took %.3fs ok=%s", time.monotonic() - t_yt, ok)
            if not ok:
                return "I couldn't open YouTube search right now."
            _remember_launch(now_playing, "youtube", query, "opened")
            if getattr(media_svc, "youtube_search_autoplay", False):
                # She meant to play it and could not pick. Say so, and hand
                # over the one thing that finishes the job.
                response = (
                    f"I couldn't pick a result for {query}, so the YouTube search is up. "
                    "Pick one with the remote."
                )
            else:
                response = f"Searching YouTube for {query}."
            return _append_route_warning(response, route_warning)

        spoken = speakable_title(video.title) or query
        t_play = time.monotonic()
        playback = media_svc.youtube_play_video(video.video_id)
        logger.info(
            "[timing] youtube_search play took %.3fs opened=%s started=%s title=%r",
            time.monotonic() - t_play, playback.opened, playback.started, playback.title,
        )
        if not playback.opened:
            return "I couldn't open that on YouTube right now."
        if playback.started is False:
            # The link went and the session never moved. Same line Stremio
            # uses, because the fix is the same: the remote.
            _remember_launch(now_playing, "youtube", spoken, "opened")
            response = f"YouTube's open on {spoken} but it didn't start on its own. Just hit OK on the remote."
            return _append_route_warning(response, route_warning)
        if playback.started is None:
            # Launched, nothing readable back. Never claim a playback that
            # was not confirmed.
            _remember_launch(now_playing, "youtube", spoken, "opened")
            response = f"I put {spoken} on YouTube, but I couldn't confirm it started."
            return _append_route_warning(response, route_warning)
        _remember_launch(now_playing, "youtube", spoken)
        response = f"Playing {spoken} on YouTube."
        return _append_route_warning(response, route_warning)

    # Navigation
    elif action == "go_home":
        media_svc.go_home()
        _forget_launch(now_playing)
        return "done"
    elif action == "go_back":
        media_svc.go_back()
        return "done"

    # Power. turn_on takes ~10-15s while the box is still on the LAN (its
    # KEYCODE_WAKEUP and the television's power-on run side by side) and up to
    # ~52s from deep standby (the TV wakes it over CEC and it resumes). Report
    # what actually happened -- a failed wake needs the remote and saying
    # otherwise strands Master Miguel.
    elif action in ("turn_on", "wake"):
        if not media_svc:
            return "media service not available"
        # Same interim line _ensure_playable uses, for the same silence. The
        # prompt promises "say you're on it and then wait", but the model's own
        # preamble is not guaranteed and a direct turn_on never reached
        # _ensure_playable, so nothing was said at all.
        if say_now:
            say_now("Hold on, waking everything up.")
        if media_svc.turn_on():
            # The box is up. The television is a separate claim, carried in
            # tv_confirmed; compare by identity, a bare Mock is truthy.
            result = getattr(media_svc, "last_wake_result", None)
            if result is not None and getattr(result, "needs_pairing", False) is True:
                logger.warning("turn_on: box up, pairing needed: %s", result.detail)
                return "the box is on, but the TV needs me approved on screen again"
            confirmed = getattr(result, "tv_confirmed", True)
            if confirmed is False:
                logger.warning("turn_on: box up, TV not showing it: %s", result.detail)
                return ("the box is on but the TV isn't showing it, give it a second "
                        "or grab the remote")
            if confirmed is None:
                logger.info("turn_on: box up, TV unconfirmed: %s", result.detail)
                return "the box is on and the TV should be coming up"
            return "TV is on"
        # A rejected pairing token and a dead TV need opposite fixes. Saying
        # "use the remote" when the real answer is "approve me on screen" sends
        # Master Miguel to the wrong one, so branch on it.
        result = getattr(media_svc, "last_wake_result", None)
        if result is not None and getattr(result, "needs_pairing", False):
            logger.warning("turn_on failed, pairing needed: %s", result.detail)
            return "the TV needs me approved on screen again"
        logger.warning("turn_on failed: %s",
                       getattr(result, "detail", None) or "no network after wake")
        return "couldn't turn the TV on, it needs the remote"
    elif action in ("turn_off", "sleep"):
        if not media_svc:
            return "media service not available"
        if media_svc.turn_off():
            if getattr(media_svc, "tv_only_standby", False) is True:
                return "TV going to standby, the box stays up"
            return "TV going to standby"
        return "couldn't put the TV to sleep"
    elif action == "power_toggle":
        if not media_svc:
            return "media service not available"
        return "TV is on" if media_svc.power_toggle() else "couldn't change the TV power state"

    elif action == "switch_hdmi":
        if not media_svc:
            return "media service not available"
        try:
            port = int(params.get("hdmi_port"))
        except (TypeError, ValueError):
            return "which HDMI port?"
        result = media_svc.switch_hdmi(port)
        if result:
            return f"switched to HDMI {port}"
        if getattr(result, "needs_pairing", False):
            return "the TV needs me approved on screen again"
        return f"couldn't switch to HDMI {port}"

    # State awareness
    elif action == "get_status":
        # NOT cec_waker._tv_is_up(). That probes the TV's REST endpoint, which
        # this set answers in standby -- cec_wake.py says so in as many words:
        # "What we must NOT do is treat the answer as 'powered on'." This branch
        # did exactly that and reported a sleeping television as on. Power now
        # comes off the CEC bus, where the TV actually said it.
        status = media_svc.room_status()
        launch = now_playing.current(status.app or "") if now_playing else None
        return _status_line(status, launch)

    return "unknown action"


def _drain_queue(q: queue.Queue) -> None:
    """Throw away everything queued, without blocking."""
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def _chunk_rms(audio_chunk: np.ndarray) -> float:
    """RMS of an int16 chunk, on the same scale as vad.energy_threshold."""
    if len(audio_chunk) == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio_chunk.astype(np.float32) ** 2)))


def _activation_clip_name(outcome: str, when: float | None = None) -> str:
    """
    Filename for a captured activation, labelled by what came of it.

    The label is the whole point: a `no_speech` or `short` clip is by
    construction a false positive, so `debug/activations/*_no_speech.wav`
    is a ready-made negatives corpus for
    `tools/score_wakeword.py --negatives`.
    """
    when = time.time() if when is None else when
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    return f"{stamp}_{int(when * 1000) % 1000:03d}_{outcome}.wav"


def _prune_clips(directory: str, max_files: int) -> list[str]:
    """Delete the oldest clips beyond max_files. Returns what was removed."""
    if max_files <= 0:
        return []
    existing = sorted(glob.glob(os.path.join(directory, "*.wav")))
    doomed = existing[:-max_files] if len(existing) > max_files else []
    for path in doomed:
        try:
            os.remove(path)
        except OSError:
            logger.debug("Could not prune activation clip %s", path)
    return doomed


def _light_memory_line(key: str, memory) -> str:
    """
    Say what she last sent the light, and say that it is what she SENT.

    The hedge lives in this string rather than in the system prompt because
    the string is what gets spoken. A prompt instruction can be forgotten six
    exchanges later; the tool result cannot.
    """
    if memory is None:
        return (
            f"I haven't touched the {key} light since I started up, and the strip "
            "can't tell me anything back, so I honestly don't know."
        )

    bits = []
    if memory.power is not None:
        bits.append("on" if memory.power else "off")
    if memory.percent is not None:
        bits.append(f"{memory.percent} percent")
    if memory.color_word:
        bits.append(memory.color_word)

    if not bits:
        return f"I don't have anything recorded for the {key} light."
    return (
        f"Last thing I sent the {key} light was {', '.join(bits)}, "
        "and I can't read the strip back so that's memory, not a reading."
    )


def _dispatch_lights(params: dict, govee_svc, light_shadow=None) -> str:
    """
    Govee light control. Module-level and service-injected for the same reason
    _dispatch_tv is: it keeps the tool layer testable without an Orchestrator.

    No ADB check and no VPN preflight here on purpose. These are cloud calls to
    Govee and have nothing to do with the Mi Box or Surfshark.

    light_shadow is injected rather than living on GoveeService on purpose.
    The dispatcher is the only layer that holds the words worth saying back:
    the CLAMPED percent and the colour Master Miguel actually said. The
    service sees an rgb tuple, and nothing in this project maps rgb back to a
    name. It also keeps the existing tests honest -- _svc() there is a bare
    Mock, so a GoveeService.get_state() would auto-stub truthy and pass on
    nothing at all.
    """
    action = params.get("action")

    if not govee_svc or not getattr(govee_svc, "enabled", False):
        return "light control isn't set up right now"

    hint = (params.get("light") or "").strip()
    key, light = govee_svc.resolve_light(hint)
    if not light:
        if hint:
            return f"I don't have a light called {hint} saved."
        return "I don't have any lights saved yet."

    if action == "light_status":
        # Nothing to ask the service: the characteristic is write-only.
        remembered = light_shadow.remembered(key) if light_shadow else None
        return _light_memory_line(key, remembered)

    if action in ("light_on", "light_off"):
        on = action == "light_on"
        result = govee_svc.set_power(key, on=on)
        if result:
            # `if result:` is a SUCCESS check. GoveeCommandResult defines
            # __bool__, so a failure is falsy and must not update memory.
            if light_shadow:
                light_shadow.record_power(key, on)
            return f"{key} lights on." if on else f"{key} lights off."
        return result.message or _LIGHTS_UNREACHABLE

    if action == "light_brightness":
        raw = params.get("brightness_percent")
        if raw is None:
            return "Tell me what brightness you want, from 1 to 100."
        percent = clamp_percent(raw)
        result = govee_svc.set_brightness(key, percent)
        if result:
            # The clamped value, because that is what the strip was sent.
            # Brightness does NOT imply power: the strip accepts this while off.
            if light_shadow:
                light_shadow.record_brightness(key, percent)
            return f"{key} lights at {percent} percent."
        return result.message or _LIGHTS_UNREACHABLE

    if action == "light_color":
        requested = (params.get("color") or "").strip()
        if not requested:
            return "Tell me what colour you want."
        rgb = resolve_color(requested)
        if not rgb:
            return f"I don't know the colour {requested}."
        result = govee_svc.set_color(key, rgb)
        if result:
            # The spoken word, not the rgb. There is no rgb-to-name map here.
            if light_shadow:
                light_shadow.record_color(key, requested)
            return f"{key} lights set to {requested}."
        return result.message or _LIGHTS_UNREACHABLE

    return "unknown action"


def _join_rooms(keys: list[str]) -> str:
    names = [f"the {k}" for k in keys]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" and {names[-1]}"


def _vacuum_status_line(status) -> str:
    battery = f"{status.battery} percent" if status.battery is not None else None
    if status.state == "error":
        detail = f": {status.error}" if status.error else ""
        return f"The vacuum has an error{detail}."
    if status.state == "docked":
        return f"The vacuum is docked{' at ' + battery if battery else ''}."
    if status.state in ("cleaning", "returning", "paused", "idle"):
        word = {"returning": "heading home", "idle": "idle"}.get(status.state, status.state)
        return f"The vacuum is {word}{', battery ' + battery if battery else ''}."
    return f"The vacuum is reachable{', battery ' + battery if battery else ''}."


def _dispatch_vacuum(params: dict, deebot_svc) -> str:
    """
    Deebot vacuum control. Module-level and service-injected like the other
    two dispatchers, so tests drive it with a bare Mock service.

    Every clean starts with a status read: a robot that is unreachable gets
    the unreachable line rather than a command fired into the void, and one
    that is already cleaning is left alone rather than restarted. The auth
    check and its one retry live inside DeebotService, not here.
    """
    action = params.get("action")

    if not deebot_svc or not getattr(deebot_svc, "enabled", False):
        return "vacuum control isn't set up right now"

    if action == "vacuum_status":
        status = deebot_svc.status()
        if not status.available:
            return status.message or _VACUUM_UNREACHABLE
        return _vacuum_status_line(status)

    if action in ("vacuum_clean_all", "vacuum_clean_rooms"):
        keys: list[str] = []
        if action == "vacuum_clean_rooms":
            hints = params.get("rooms") or []
            if isinstance(hints, str):
                hints = [hints]
            hints = [str(h).strip() for h in hints if str(h).strip()]
            if not hints:
                return "Tell me which rooms to clean."
            for hint in hints:
                key, room = deebot_svc.resolve_room(hint)
                if not room:
                    return f"I don't have a room called {hint} on the vacuum's map."
                if key not in keys:
                    keys.append(key)

        status = deebot_svc.status()
        if not status.available:
            return status.message or _VACUUM_UNREACHABLE
        if status.state == "cleaning":
            return "The vacuum's already cleaning."

        if action == "vacuum_clean_all":
            result = deebot_svc.clean_all()
            if result:
                return "Cleaning the whole house."
            return result.message or _VACUUM_UNREACHABLE

        result = deebot_svc.clean_rooms(keys)
        if result:
            return f"Cleaning {_join_rooms(keys)}."
        return result.message or _VACUUM_UNREACHABLE

    if action == "vacuum_stop":
        result = deebot_svc.stop()
        if result:
            return "Vacuum stopped."
        return result.message or _VACUUM_UNREACHABLE

    if action == "vacuum_dock":
        result = deebot_svc.dock()
        if result:
            return "Sending the vacuum home."
        return result.message or _VACUUM_UNREACHABLE

    return "unknown action"


class Orchestrator:
    def __init__(self, config: dict):
        self.config = config

        from core.audio_pipeline import AudioPipeline
        from core.wake_word import WakeWordDetector
        from core.vad import VAD
        from hardware.led_controller import LEDController
        from services.llm import LLMService
        from services.stt import STTService
        from services.tts import TTSService

        # Initialize all components. Phase 1 builds everything that only needs
        # `config` — these don't depend on each other, so they run in parallel
        # instead of one after another. See _build_parallel for why threads work
        # here despite the GIL. This alone is most of the boot-time win: the
        # slowest of these (Kokoro TTS, Silero VAD, the wake-word model) used to
        # add up; now boot takes as long as the slowest one, not the sum.
        logger.info("Initializing components (parallel boot)...")
        boot_start = time.monotonic()
        _warm_shared_imports()

        media_enabled = bool(config.get("media", {}).get("enabled"))

        phase1 = {
            "audio": lambda: AudioPipeline(config),
            "wake_word": lambda: WakeWordDetector(config),
            "vad": lambda: VAD(config),
            "stt": lambda: STTService(config),
            "llm": lambda: LLMService(config),
            "tts": lambda: TTSService(config),
            "leds": lambda: LEDController(config),
            "govee": lambda: GoveeService(config),
            # Construction only, no network: DeebotService authenticates lazily
            # on the first command, and self-disables without credentials.
            "deebot": lambda: DeebotService(config),
        }
        if media_enabled:
            # Construction only here — no network. MediaService.connect() is a
            # network call and belongs in phase 2 with the other network work.
            phase1["media"] = lambda: MediaService(config)

        built = _build_parallel(phase1)

        self.audio = built["audio"]
        self.wake_word = built["wake_word"]
        self.vad = built["vad"]
        self.stt = built["stt"]
        self.llm = built["llm"]
        self.tts = built["tts"]
        self.leds = built["leds"]
        self.govee_service = built["govee"]
        self.deebot_service = built["deebot"]

        if media_enabled:
            self.media_service = built["media"]
            self.surfshark_service = SurfsharkService(config, self.media_service)
        else:
            self.media_service = None
            self.surfshark_service = None

        # What she last put on the screen. The box knows which app is up and
        # whether something is playing, but never what -- she is the only one
        # who knows that, because she launched it.
        self.now_playing = NowPlaying()

        # What she last told the lights. The Govee characteristic is
        # write-only, so this is the only answer that exists for them.
        self.light_shadow = LightShadow()

        # Phase 2: the network calls. MediaService.connect() (ADB connect +
        # device discovery) and StremioService's startup login + library sync
        # are both blocking I/O and don't depend on each other, so they also
        # run side by side rather than back to back.
        phase2 = {
            "stremio": lambda: StremioService(config, media_service=self.media_service),
        }
        if self.media_service is not None:
            phase2["media_connected"] = self.media_service.connect

        done2 = _build_parallel(phase2)

        self.stremio_service = done2["stremio"]
        if self.media_service is not None:
            connected = done2["media_connected"]
            logger.info("Mi BOX S connected" if connected else "Mi BOX S not reachable at startup")

        self._background_stop = threading.Event()
        self._stremio_sync_thread: threading.Thread | None = None

        sync_interval = int(config.get("stremio", {}).get("library_sync_interval_minutes", 60))
        if self.stremio_service.can_sync() and sync_interval > 0:
            self._stremio_sync_thread = threading.Thread(
                target=self._stremio_sync_loop,
                args=(sync_interval,),
                daemon=True,
            )
            self._stremio_sync_thread.start()
            logger.info("Stremio background sync started (%d min interval)", sync_interval)

        # Register tool handler so LLM can dispatch control_tv / control_lights
        self.llm.tool_handler = self._handle_tool_call

        # Tell the LLM which lights actually loaded, so the system prompt reflects
        # reality rather than raw config (lights missing a mac/sku are skipped).
        self.llm.light_names = list(self.govee_service.lights)
        self.llm.default_light = self.govee_service.default_light

        # Same for the vacuum's rooms. Cached ids in config.yaml can go stale
        # after a remap; the drift check is a best-effort background warning,
        # never a blocker and never a config rewrite.
        self.llm.vacuum_room_names = list(self.deebot_service.rooms)
        if self.deebot_service.enabled and self.deebot_service.rooms:
            threading.Thread(
                target=self.deebot_service.check_room_drift,
                name="deebot-room-drift",
                daemon=True,
            ).start()

        # Activation sound tiering + echo gating. The first wake of a run gets a
        # long personality line, every wake after gets a short one, and playback
        # never blocks the microphone.
        sounds_cfg = config.get("sounds", {})
        self._wake_count = 0
        self._barge_in_rms = float(sounds_cfg.get("barge_in_energy_threshold", 900))
        self._onset_guard_s = float(sounds_cfg.get("activation_onset_guard_ms", 150)) / 1000.0
        self._barge_in_guard_s = float(sounds_cfg.get("barge_in_guard_ms", 120)) / 1000.0
        self._bootup_dir = sounds_cfg.get("bootup_dir", "sounds/bootup")

        # A recording shorter than this is not a command, whatever the VAD said.
        # _record_speech has always promised this in its docstring; now it keeps it.
        self._min_recording_s = float(config.get("vad", {}).get("min_recording", 0.0))

        # Optional capture of the audio around each activation, off by default.
        # Flip it on to collect real false positives, then score them with
        # tools/score_wakeword.py --negatives.
        ww_cfg = config.get("wake_word", {}) or {}
        capture_cfg = ww_cfg.get("capture", {}) or {}
        self._capture_enabled = bool(capture_cfg.get("enabled", False))
        self._capture_dir = capture_cfg.get("dir", "debug/activations")
        self._capture_max_files = int(capture_cfg.get("max_files", 200))
        pre_seconds = float(capture_cfg.get("pre_seconds", 2.0))
        self._capture_ring = None
        if self._capture_enabled:
            ring_len = max(1, int(pre_seconds * self.audio.sample_rate / self.audio.chunk_samples))
            self._capture_ring = collections.deque(maxlen=ring_len)
            logger.info(
                "Activation capture on: %s (%.1fs pre-roll, keeping %d clips)",
                self._capture_dir, pre_seconds, self._capture_max_files,
            )

        # Barge-in over a reply is by wake word, not by loudness: the mic hears
        # her through the speaker at well over any energy threshold (that is
        # why activation_blocking is on), but the detector is trained on one
        # word in his voice and scores hers near zero. The score it needs
        # while she is talking can sit above the idle threshold.
        self._barge_in_threshold = float(
            ww_cfg.get("barge_in_threshold", ww_cfg.get("threshold", 0.5))
        )

        # State
        self._running = False
        # Set by the reply listener (wake word heard mid-reply) or the "stop"
        # command. Every stage of the speaking pipeline checks it.
        self._interrupted = threading.Event()
        # Text of the chunk being played right now, so the reply listener can
        # ignore a wake-word hit while she is saying her own name.
        self._speaking_text = ""
        # The current turn's TTS queue, set only while _generate_and_speak runs.
        # _say_now uses it to speak from inside a long tool call.
        self._active_tts_queue = None
        self._last_record_outcome = "ok"  # "ok" | "no_speech" | "short"

        logger.info("All components initialized in %.1fs", time.monotonic() - boot_start)

    def _play_bootup_sound(self):
        """Pick a random WAV from `sounds.bootup_dir` and play it."""
        import soundfile as sf

        bootup_dir = self._bootup_dir
        if not os.path.isabs(bootup_dir):
            bootup_dir = os.path.join(os.path.dirname(__file__), "..", bootup_dir)
        bootup_dir = os.path.normpath(bootup_dir)
        files = glob.glob(os.path.join(bootup_dir, "*.wav"))
        if not files:
            logger.debug("No bootup sounds found in %s — skipping", bootup_dir)
            return
        chosen = random.choice(files)
        logger.info("Bootup sound: %s", os.path.basename(chosen))
        try:
            audio, sr = sf.read(chosen, dtype="float32")
            self.audio.play_audio(audio, sr, blocking=True)
        except Exception:
            logger.exception("Failed to play bootup sound %s", chosen)

    def run(self):
        """Main loop. Blocks until interrupted."""
        self._running = True
        self.leds.set_state("idle")

        print("\n" + "=" * 50)
        print("  🌴 Project California is running!")
        print(f"  Wake word: {self.config['wake_word']['model']}")
        print(f"  STT: {self.config['stt']['provider']}")
        print(f"  LLM: {self.config['llm']['provider']}")
        print(f"  TTS: {self.config['tts']['provider']}")
        print("=" * 50 + "\n")

        mic_stream = self.audio.create_mic_stream()
        mic_stream.start()
        # The process's first device open is ~2s on this laptop (driver init;
        # every later open is ~200ms). Pay it here, under the greeting, rather
        # than in front of the first acknowledgement clip.
        self.audio.open_speaker()
        try:
            self._play_bootup_sound()
        finally:
            self.audio.close_speaker()
        # The bootup line went out of the speaker and straight into the mic
        # buffer. Do not hand it to the wake-word detector.
        self._drain_mic(mic_stream, "bootup sound")

        try:
            while self._running:
                self._idle_loop(mic_stream)
        except KeyboardInterrupt:
            print("\n\n  Shutting down...")
        finally:
            self._background_stop.set()
            if self._stremio_sync_thread:
                self._stremio_sync_thread.join(timeout=2)
            self.deebot_service.close()
            mic_stream.stop()
            mic_stream.close()
            self.leds.off()
            print("  Goodbye! 🌴\n")

    def _idle_loop(self, mic_stream):
        """
        IDLE state: Feed audio to wake word detector.
        Transitions to LISTENING when wake word is detected.
        """
        self.leds.set_state("idle")

        # Read one chunk from mic
        audio_bytes, overflowed = mic_stream.read(self.audio.chunk_samples)
        if overflowed:
            logger.warning("Audio buffer overflow")

        audio_chunk = self.audio.bytes_to_numpy(audio_bytes)

        # Keep the last couple of seconds so a capture can show what she
        # actually heard, including the audio *before* the wake fired.
        if self._capture_ring is not None:
            self._capture_ring.append(audio_chunk)

        # Feed to wake word detector
        if self.wake_word.process_audio(audio_chunk):
            # Wake word detected!
            pre_roll = list(self._capture_ring) if self._capture_ring is not None else []

            # The speaker is opened once here and held through the whole
            # exchange — acknowledgement, recording, thinking, reply, and any
            # turn chained onto it by a barge-in — then closed with a short
            # silent tail. See AudioPipeline.open_speaker for why.
            self.audio.open_speaker()
            try:
                barged_in = self._handle_activation(mic_stream, pre_roll=pre_roll)
                while barged_in:
                    # He said her name over the reply. The listener thread
                    # was reading the mic, so the buffer is nearly fresh; the
                    # drain covers the moments between it stopping and this.
                    self._drain_mic(mic_stream, "barge-in")
                    barged_in = self._handle_activation(mic_stream)
            finally:
                self.audio.close_speaker()

            # The whole turn — thinking, and every sentence she spoke — went
            # into the mic buffer while nobody was reading it. Feeding her own
            # reply back to the wake-word detector is a false-positive machine,
            # so drop it. One drain here covers every way an activation ends.
            self._drain_mic(mic_stream, "response playback")
            if self._capture_ring is not None:
                self._capture_ring.clear()

    def _handle_activation(self, mic_stream, pre_roll=None) -> bool:
        """
        Handle a wake word activation:
        1. Start the activation line (does not block the microphone)
        2. Record user speech, gating out the line's own bleed
        3. Transcribe
        4. Query LLM (streaming)
        5. Speak response (streaming), listening for the wake word throughout

        If nothing was ever said (a false wake, or he changed his mind), step 2
        returns None and this goes quietly back to idle — no Whisper call, no
        LLM call, no spoken reply. Silence is not an input.

        Returns True when the reply was cut off by the wake word, so the caller
        goes straight into another activation instead of back to idle.
        """
        logger.info("--- Wake word activated ---")

        # A stop from the previous turn (barge-in, "stop") stays in force
        # until the next turn arms the speaker again. This is that.
        self.audio.reset_playback()

        # First wake of the run gets a long line, the rest get short ones.
        tier = resolve_tier(self._wake_count)
        self._wake_count += 1

        # Playback does not block: recording starts now and the EchoGate inside
        # _record_speech discards whatever the mic picks up of the line itself.
        playback = self.audio.play_activation_sound(tier)
        logger.info(
            "Activation tier=%s line=%s (%.2fs)",
            tier,
            playback.name if playback else "none",
            playback.duration if playback else 0.0,
        )

        # --- LISTENING: Record until silence ---
        self.leds.set_state("listening")
        audio_data = self._record_speech(mic_stream, playback)

        if audio_data is None or len(audio_data) == 0:
            logger.info("No speech after the wake word — returning to idle without asking")
            self._capture_activation(pre_roll, None, self._last_record_outcome)
            self.audio.stop_playback()
            # Clear the detector's frame counter and openWakeWord's feature
            # buffers so the same stale audio cannot immediately re-fire.
            self.wake_word.reset()
            # Do not let a false positive burn the one long cold-open line.
            self._wake_count = max(0, self._wake_count - 1)
            self.leds.set_state("idle")
            return False

        self._capture_activation(pre_roll, audio_data, self._last_record_outcome)

        # --- PROCESSING: STT → LLM ---
        self.leds.set_state("thinking")

        # Convert to WAV and transcribe
        wav_bytes = self.audio.numpy_to_wav_bytes(audio_data)
        transcript = self.stt.transcribe(wav_bytes)

        # Safety net for any of the activation line that survived the audio trim
        # and reached Whisper as a prefix. Conservative by design: see
        # services.activation_phrases.strip_activation_echo.
        if playback and playback.text:
            cleaned = strip_activation_echo(transcript, playback.text)
            if cleaned != transcript:
                logger.info("Stripped activation echo: %r -> %r", transcript, cleaned)
                transcript = cleaned

        if not transcript or transcript.strip() == "":
            logger.info("Empty transcription, returning to idle")
            return False

        logger.info(f"User said: '{transcript}'")
        print(f"\n  👤 You: {transcript}")

        # Check for special commands
        if self._handle_command(transcript):
            return False

        # --- STREAMING: LLM → Sentence Chunker → TTS ---
        self.leds.set_state("thinking")
        return self._stream_response(transcript, mic_stream)

    def _drain_mic(self, mic_stream, why: str):
        """
        Drop whatever the mic buffered while we were not listening.

        See AudioPipeline.drain_mic_stream. Anywhere the orchestrator stops
        reading the stream — playing a line, thinking, speaking — the buffer
        fills with the room and with California herself, and the next read
        would hand that back as if it had just happened.
        """
        # The logging lives inside the try on purpose: a diagnostic must never
        # be the thing that breaks a turn.
        try:
            dropped = self.audio.drain_mic_stream(mic_stream)
            if dropped:
                logger.debug(
                    "Dropped %.2fs of stale mic audio (%s)",
                    dropped / self.audio.sample_rate, why,
                )
        except Exception:
            logger.exception("Could not drain the mic buffer (%s)", why)

    def _capture_activation(self, pre_roll, recorded, outcome: str):
        """
        Write the audio around one activation to disk, labelled by outcome.

        Diagnostics only, and off unless wake_word.capture.enabled is set. It
        must never be able to break a turn, hence the blanket except.
        """
        if not self._capture_enabled:
            return

        try:
            parts = list(pre_roll or [])
            if recorded is not None and len(recorded):
                parts.append(recorded)
            if not parts:
                return

            os.makedirs(self._capture_dir, exist_ok=True)
            path = os.path.join(self._capture_dir, _activation_clip_name(outcome))
            with open(path, "wb") as fh:
                fh.write(self.audio.numpy_to_wav_bytes(np.concatenate(parts)))
            _prune_clips(self._capture_dir, self._capture_max_files)
            logger.info("Captured activation clip: %s", path)
        except Exception:
            logger.exception("Failed to capture activation clip")

    def _record_speech(self, mic_stream, playback=None) -> np.ndarray | None:
        """
        Record audio until VAD detects silence.
        Returns numpy array of recorded audio (int16), or None if too short.

        Recording starts the moment the wake word fires, while the activation
        line is still playing, so the first chunks are California's own voice
        coming back through the speaker. An EchoGate holds the VAD clock until
        the line ends or Master Miguel talks over it, and those chunks are then
        dropped so Whisper never sees them.

        Returns None when he never spoke ("no_speech", the VAD's grace window
        expired) or when what he said is shorter than vad.min_recording. Both
        are dropped before STT — an empty turn is cheaper than a hallucinated one.
        """
        self._last_record_outcome = "ok"

        # Start from what the room is doing NOW. With a blocking activation
        # line this is the whole fix: the line played out of the speaker and
        # into the mic buffer, and without this the first thing "recorded" is
        # her own voice read back out of it — which is exactly what got
        # transcribed and answered.
        self._drain_mic(mic_stream, "activation line")

        window = playback.duration if playback else 0.0
        gate = EchoGate(window, self._barge_in_rms, self._onset_guard_s)

        self.vad.start_recording()
        started = time.monotonic()
        chunks = []
        trim_from = 0
        stop_reason = "continue"

        while True:
            audio_bytes, overflowed = mic_stream.read(self.audio.chunk_samples)
            audio_chunk = self.audio.bytes_to_numpy(audio_bytes)
            chunks.append(audio_chunk)

            if not gate.armed:
                if not gate.update(time.monotonic() - started, _chunk_rms(audio_chunk)):
                    continue
                if gate.barged_in:
                    # He started talking over the line. Cut her off mid-word the
                    # way a person would, and back the trim up a little so the
                    # first phoneme is not clipped.
                    self.audio.stop_playback()
                    guard_chunks = max(
                        0, int(self._barge_in_guard_s * self.audio.sample_rate)
                        // self.audio.chunk_samples
                    )
                    trim_from = max(0, len(chunks) - 1 - guard_chunks)
                    logger.info("Barge-in: cut activation line short")
                else:
                    # The line finished on its own, so everything before this
                    # chunk was speaker bleed and none of it was him.
                    trim_from = len(chunks) - 1
                # min_recording and the silence timer should measure his speech,
                # not the activation line, so restart the VAD clock here.
                self.vad.start_recording()
                continue

            should_stop, reason = self.vad.should_stop_recording(audio_chunk)
            if should_stop:
                logger.info(f"Recording stopped: {reason}")
                stop_reason = reason
                break

        if stop_reason == "no_speech":
            # The grace window ran out with nothing said. Almost always a false
            # wake; occasionally he called her and thought better of it. Either
            # way there is nothing to transcribe.
            self._last_record_outcome = "no_speech"
            logger.info(
                "Nothing said within %.1fs of the wake word — dropping the turn",
                getattr(self.vad, "speech_timeout", 0.0),
            )
            return None

        kept = chunks[trim_from:]
        if not kept:
            self._last_record_outcome = "short"
            return None

        audio_data = np.concatenate(kept)
        duration = len(audio_data) / self.audio.sample_rate

        if duration < self._min_recording_s:
            # The docstring has always promised this; now it is true. Mostly
            # reachable via the barge-in trim, which can leave a few chunks.
            self._last_record_outcome = "short"
            logger.info(
                "Recording too short (%.2fs < %.2fs) — dropping the turn",
                duration, self._min_recording_s,
            )
            return None

        logger.info(
            "Recorded %.1fs of audio (dropped %.1fs of activation overlap)",
            duration,
            sum(len(c) for c in chunks[:trim_from]) / self.audio.sample_rate,
        )

        return audio_data

    def _stream_response(self, user_text: str, mic_stream=None) -> bool:
        """
        The streaming pipeline: LLM → Sentence Chunker → TTS → Speaker.

        This is where the magic happens. Instead of waiting for the full LLM
        response, we:
        1. Stream tokens from the LLM
        2. Accumulate them into sentences
        3. Send each sentence to TTS immediately
        4. Play audio while the LLM keeps generating

        Architecture:
          [LLM stream] → [sentence_chunker] → [tts_queue] → [tts_worker thread]
          [mic_stream] → [_reply_listener thread] → wake word → interrupt

        The listener is what makes her interruptible: it keeps reading the mic
        while she talks and runs the wake-word detector on it. A hit sets
        `_interrupted`, which every stage checks, and stops the speaker.

        Returns True when the reply was cut off that way. The caller then
        treats it as a fresh activation — "California, no, the other one" is
        one motion.
        """
        # Queue for sentences waiting to be spoken
        tts_queue: queue.Queue[str | None] = queue.Queue()
        full_response_parts = []
        self._interrupted.clear()

        # A tool call runs synchronously on THIS thread inside the LLM stream,
        # while _tts_worker drains tts_queue on its own. So a long-running tool
        # can speak by pushing here: the audio plays while it is still blocked.
        # Nothing else can reach the queue, hence the handoff on self.
        self._active_tts_queue = tts_queue

        # Listen for the wake word for as long as this turn is speaking. Only
        # this thread reads the mic until it is stopped below, and it is
        # stopped before _idle_loop reads again, so the stream never has two
        # readers.
        listener_stop = threading.Event()
        listener = None
        if mic_stream is not None:
            listener = threading.Thread(
                target=self._reply_listener,
                args=(mic_stream, listener_stop),
                name="reply-listener",
                daemon=True,
            )
            listener.start()

        # Start TTS worker thread
        tts_thread = threading.Thread(
            target=self._tts_worker,
            args=(tts_queue,),
            daemon=True,
        )
        tts_thread.start()

        try:
            # Stream LLM → accumulate sentences → enqueue for TTS
            from services.sentence_chunker import chunk_sentences

            token_stream = self.llm.stream_response(user_text)
            first_sentence = True

            for sentence in chunk_sentences(token_stream):
                if self._interrupted.is_set():
                    # Close the LLM stream now rather than whenever the
                    # generator is collected; stream_response records what was
                    # generated so far in history on the way out.
                    logger.info("Barge-in: abandoning the rest of the reply")
                    token_stream.close()
                    break

                full_response_parts.append(sentence)

                if first_sentence:
                    self.leds.set_state("speaking")
                    first_sentence = False

                tts_queue.put(sentence)

        except Exception as e:
            logger.error(f"Streaming error: {e}")
            tts_queue.put("Sorry, something went wrong.")

        finally:
            # Drop the handoff before the queue is closed, so a stray say_now
            # from a later thread cannot push into a finished turn.
            self._active_tts_queue = None
            # Signal TTS worker to stop. No timeout: a long reply is spoken to
            # the end, and the only way to cut it short is the wake word or
            # "stop", both of which make the worker exit within a block.
            tts_queue.put(None)
            tts_thread.join()
            listener_stop.set()
            if listener is not None:
                listener.join()

        full_response = " ".join(full_response_parts)
        if full_response:
            print(f"  🌴 California: {full_response}")

        return self._interrupted.is_set()

    def _reply_listener(self, mic_stream, stop: threading.Event) -> None:
        """
        Read the mic while she is talking and watch for the wake word.

        Runs on its own thread for the length of one reply. A hit means he is
        talking over her: stop the speaker at once and flag the turn, and the
        orchestrator chains straight into a new activation.

        Two guards:
        - `wake_word.barge_in_threshold` (default: the idle threshold), because
          the mic is also hearing her through the speaker the whole time.
        - Her own name. If the chunk playing right now contains "California",
          a hit is ignored: she must not wake herself by introducing herself.

        Anything that goes wrong in here is logged and ends the listener; it
        can never take the turn down with it.
        """
        peak = 0.0
        started = time.monotonic()
        try:
            while not stop.is_set():
                audio_bytes, _ = mic_stream.read(self.audio.chunk_samples)
                chunk = self.audio.bytes_to_numpy(audio_bytes)
                hit = self.wake_word.process_audio(chunk, threshold=self._barge_in_threshold)
                peak = max(peak, float(getattr(self.wake_word, "last_score", 0.0) or 0.0))
                if not hit:
                    continue
                if "california" in (self._speaking_text or "").lower():
                    logger.info("Wake word scored while she was saying her own name — ignored")
                    continue
                logger.info("Barge-in: wake word during reply")
                self._interrupted.set()
                self.audio.stop_playback()
                return
        except Exception:
            logger.exception("Reply listener stopped early")
        finally:
            # The number to read when "California" over her did nothing: how
            # close the detector got against barge_in_threshold. Measured
            # 2026-09-16, her own voice through the mic peaks at 0.053, so
            # anything well above that was him.
            logger.info(
                "Reply listener: peak wake score %.3f over %.1fs (barge_in_threshold %.2f)",
                peak, time.monotonic() - started, self._barge_in_threshold,
            )

    def _tts_worker(self, tts_queue: queue.Queue):
        """
        Pulls sentences from tts_queue, synthesizes audio, pushes to audio_queue.
        A separate _audio_player_worker thread consumes audio_queue and plays it.
        This means synthesis of sentence N+1 overlaps with playback of sentence N.
        """
        audio_queue: queue.Queue[tuple[np.ndarray, int, str] | None] = queue.Queue(maxsize=2)

        # Start the audio playback thread
        player_thread = threading.Thread(
            target=self._audio_player_worker,
            args=(audio_queue,),
            daemon=True,
        )
        player_thread.start()

        try:
            while True:
                sentence = tts_queue.get()

                if sentence is None:
                    break

                if self._interrupted.is_set():
                    _drain_queue(tts_queue)
                    break

                try:
                    audio_data, sample_rate = self.tts.synthesize(sentence)
                    # The stop may have landed during synthesis. The player
                    # may already be gone, so do not hand it anything.
                    if len(audio_data) > 0 and not self._interrupted.is_set():
                        audio_queue.put((audio_data, sample_rate, sentence))
                except Exception as e:
                    logger.error(f"TTS synthesis error: {e}")

        finally:
            # Signal player to stop and wait for it to finish playing. After an
            # interrupt the player has exited, so clear its queue first or the
            # bounded put below would block forever.
            if self._interrupted.is_set():
                _drain_queue(audio_queue)
            audio_queue.put(None)
            player_thread.join()

    def _audio_player_worker(self, audio_queue: queue.Queue):
        """
        Pulls (audio_data, sample_rate) from audio_queue and plays them back-to-back.
        Blocks on each playback so order is preserved.
        Runs until it receives None, or until the turn is interrupted.
        """
        while True:
            item = audio_queue.get()

            if item is None:
                break

            if self._interrupted.is_set():
                _drain_queue(audio_queue)
                break

            audio_data, sample_rate, text = item
            # Published for the reply listener's own-name guard.
            self._speaking_text = text
            try:
                self.audio.play_audio(audio_data, sample_rate, blocking=True)
            except Exception as e:
                logger.error(f"TTS playback error: {e}")
            finally:
                self._speaking_text = ""


    def _say_now(self, text: str) -> None:
        """
        Speak a line mid-turn, without ending it.

        For tool calls that block long enough to read as a hang -- a CEC wake is
        ~25s. The sentence is appended to the turn's live TTS queue, so it plays
        in order after whatever the LLM already said and before whatever it says
        once the tool returns. No-op outside a streaming turn.
        """
        active = getattr(self, "_active_tts_queue", None)
        if active is None or not text:
            return
        logger.info("Interim line while a tool call runs: %s", text)
        active.put(text)

    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch tool calls from the LLM."""
        if tool_name == "control_tv":
            return _dispatch_tv(
                tool_input,
                self.media_service,
                self.stremio_service,
                self.surfshark_service,
                self.config.get("youtube_playlists", {}),
                self.config.get("youtube_playlist_aliases") or {},
                say_now=self._say_now,
                now_playing=self.now_playing,
            )
        if tool_name == "control_lights":
            return _dispatch_lights(tool_input, self.govee_service, self.light_shadow)
        if tool_name == "control_vacuum":
            return _dispatch_vacuum(tool_input, self.deebot_service)
        return "unknown tool"

    def _stremio_sync_loop(self, interval_minutes: int):
        interval_seconds = max(60, interval_minutes * 60)
        while not self._background_stop.wait(interval_seconds):
            try:
                self.stremio_service.sync_library()
            except Exception as exc:
                logger.warning("Background Stremio sync failed: %s", exc)

    def _handle_command(self, transcript: str) -> bool:
        """
        Handle special voice commands.
        Returns True if a command was handled (skip LLM).
        """
        lower = transcript.lower().strip()

        # Clear conversation history
        if lower in ("clear history", "forget everything", "reset conversation", "new conversation"):
            self.llm.clear_history()
            self._speak_direct("Conversation history cleared. Fresh start!")
            return True

        # Stop / shut up. Reaching here at all means the wake word already cut
        # the reply short; this makes sure nothing queued behind it plays.
        if lower in ("stop", "shut up", "be quiet", "cancel"):
            self._interrupted.set()
            self.audio.stop_playback()
            return True

        return False

    def _speak_direct(self, text: str):
        """Speak a message directly (not streamed through LLM)."""
        self.leds.set_state("speaking")
        print(f"  🌴 California: {text}")
        try:
            audio, sr = self.tts.synthesize(text)
            if len(audio) > 0:
                self.audio.play_audio(audio, sr, blocking=True)
        except Exception as e:
            logger.error(f"Direct speak error: {e}")

    def stop(self):
        """Stop the orchestrator."""
        self._running = False
        self._background_stop.set()
