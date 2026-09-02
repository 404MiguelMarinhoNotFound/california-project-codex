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

from services.activation_phrases import EchoGate, resolve_tier, strip_activation_echo
from services.govee_service import GoveeService, clamp_percent, resolve_color
from services.media_service import MediaService
from services.stremio_service import AUTOPLAY_FALLBACK_LINE, StremioService
from services.surfshark_service import SurfsharkService
from services.youtube_playlist_resolver import resolve_playlist_choice

logger = logging.getLogger(__name__)

_LIGHTS_UNREACHABLE = "I couldn't reach your lights just now."

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


def _dispatch_tv(params: dict, media_svc, stremio_svc, surfshark_svc, youtube_playlists: dict) -> str:
    action = params.get("action")
    route_warning = None

    requires_tv = {
        "play_pause", "stop", "next", "prev", "fast_forward", "rewind",
        "volume_up", "volume_down", "volume_set", "mute",
        "launch_app", "go_home", "go_back",
        "power_toggle", "sleep", "wake",
        "get_status", "youtube_playlist", "youtube_search",
        "stremio_play", "stremio_continue",
    }

    if action in requires_tv:
        if not media_svc:
            return "media service not available"
        if not media_svc.ensure_connected():
            return "TV is off or unreachable right now"

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
            return _append_route_warning(response, route_warning)
        return result.message or AUTOPLAY_FALLBACK_LINE

    # YouTube
    elif action == "youtube_playlist":
        t_yt = time.monotonic()
        playlist_id = (params.get("playlist_id") or "").strip()
        playlist_name = (params.get("playlist_name") or "").strip()
        matched_key = None

        if not playlist_id:
            matched_key, playlist_id = resolve_playlist_choice(playlist_name, youtube_playlists)

        if not playlist_id:
            fallback_name = playlist_name or "that"
            return f"I don't have a {fallback_name} playlist saved. Want me to search YouTube for it?"

        ok = media_svc.youtube_playlist(playlist_id)
        logger.info("[timing] youtube_playlist dispatch took %.3fs ok=%s", time.monotonic() - t_yt, ok)
        if not ok:
            return "I couldn't open that YouTube playlist right now."
        if matched_key:
            response = f"Opening your {matched_key} playlist on YouTube."
            return _append_route_warning(response, route_warning)
        response = "Opening that YouTube playlist."
        return _append_route_warning(response, route_warning)

    elif action == "youtube_search":
        t_yt = time.monotonic()
        query = (params.get("query") or "").strip()
        if not query:
            return "Tell me what to search for on YouTube."
        ok = media_svc.youtube_search(query)
        logger.info("[timing] youtube_search dispatch took %.3fs ok=%s", time.monotonic() - t_yt, ok)
        if not ok:
            return "I couldn't open YouTube search right now."
        response = f"Searching YouTube for {query}."
        return _append_route_warning(response, route_warning)

    # Navigation
    elif action == "go_home":
        media_svc.go_home()
        return "done"
    elif action == "go_back":
        media_svc.go_back()
        return "done"

    # Power
    elif action == "power_toggle":
        media_svc.power_toggle()
        return "power toggled"
    elif action == "sleep":
        media_svc.sleep()
        return "TV going to standby"
    elif action == "wake":
        media_svc.wake()
        return "wake signal sent"

    # State awareness
    elif action == "get_status":
        app = media_svc.get_current_app()
        session = media_svc.get_media_session()
        return f"Current app: {app}. Media session: {session}"

    return "unknown action"


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


def _dispatch_lights(params: dict, govee_svc) -> str:
    """
    Govee light control. Module-level and service-injected for the same reason
    _dispatch_tv is: it keeps the tool layer testable without an Orchestrator.

    No ADB check and no VPN preflight here on purpose. These are cloud calls to
    Govee and have nothing to do with the Mi Box or Surfshark.
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

    if action in ("light_on", "light_off"):
        result = govee_svc.set_power(key, on=(action == "light_on"))
        if result:
            return f"{key} lights on." if action == "light_on" else f"{key} lights off."
        return result.message or _LIGHTS_UNREACHABLE

    if action == "light_brightness":
        raw = params.get("brightness_percent")
        if raw is None:
            return "Tell me what brightness you want, from 1 to 100."
        percent = clamp_percent(raw)
        result = govee_svc.set_brightness(key, percent)
        if result:
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
            return f"{key} lights set to {requested}."
        return result.message or _LIGHTS_UNREACHABLE

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

        # Initialize all components
        logger.info("Initializing components...")
        self.audio = AudioPipeline(config)
        self.wake_word = WakeWordDetector(config)
        self.vad = VAD(config)
        self.stt = STTService(config)
        self.llm = LLMService(config)
        self.tts = TTSService(config)
        self.leds = LEDController(config)

        # Mi BOX S / Media control
        if config.get("media", {}).get("enabled"):
            self.media_service = MediaService(config)
            connected = self.media_service.connect()
            logger.info("Mi BOX S connected" if connected else "Mi BOX S not reachable at startup")
            self.surfshark_service = SurfsharkService(config, self.media_service)
        else:
            self.media_service = None
            self.surfshark_service = None

        # Govee lights. Constructed unconditionally — the service self-disables
        # when config is off or GOVEE_API_KEY is missing, so there is no None branch.
        self.govee_service = GoveeService(config)

        # Stremio library/progress + deep-link control
        self.stremio_service = StremioService(config, media_service=self.media_service)
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

        # Activation sound tiering + echo gating. The first wake of a run gets a
        # long personality line, every wake after gets a short one, and playback
        # never blocks the microphone.
        sounds_cfg = config.get("sounds", {})
        self._wake_count = 0
        self._barge_in_rms = float(sounds_cfg.get("barge_in_energy_threshold", 900))
        self._onset_guard_s = float(sounds_cfg.get("activation_onset_guard_ms", 150)) / 1000.0
        self._barge_in_guard_s = float(sounds_cfg.get("barge_in_guard_ms", 120)) / 1000.0

        # A recording shorter than this is not a command, whatever the VAD said.
        # _record_speech has always promised this in its docstring; now it keeps it.
        self._min_recording_s = float(config.get("vad", {}).get("min_recording", 0.0))

        # Optional capture of the audio around each activation, off by default.
        # Flip it on to collect real false positives, then score them with
        # tools/score_wakeword.py --negatives.
        capture_cfg = config.get("wake_word", {}).get("capture", {}) or {}
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

        # State
        self._running = False
        self._interrupted = False  # Barge-in flag
        self._last_record_outcome = "ok"  # "ok" | "no_speech" | "short"

        logger.info("All components initialized successfully")

    def _play_bootup_sound(self):
        """Pick a random WAV from sounds/bootup/ and play it."""
        import soundfile as sf

        bootup_dir = os.path.join(os.path.dirname(__file__), "..", "sounds", "bootup")
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
        self._play_bootup_sound()

        try:
            while self._running:
                self._idle_loop(mic_stream)
        except KeyboardInterrupt:
            print("\n\n  Shutting down...")
        finally:
            self._background_stop.set()
            if self._stremio_sync_thread:
                self._stremio_sync_thread.join(timeout=2)
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
            self._handle_activation(mic_stream, pre_roll=pre_roll)

    def _handle_activation(self, mic_stream, pre_roll=None):
        """
        Handle a wake word activation:
        1. Start the activation line (does not block the microphone)
        2. Record user speech, gating out the line's own bleed
        3. Transcribe
        4. Query LLM (streaming)
        5. Speak response (streaming)

        If nothing was ever said (a false wake, or he changed his mind), step 2
        returns None and this goes quietly back to idle — no Whisper call, no
        LLM call, no spoken reply. Silence is not an input.
        """
        logger.info("--- Wake word activated ---")

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
            return

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
            return

        logger.info(f"User said: '{transcript}'")
        print(f"\n  👤 You: {transcript}")

        # Check for special commands
        if self._handle_command(transcript):
            return

        # --- STREAMING: LLM → Sentence Chunker → TTS ---
        self.leds.set_state("thinking")
        self._stream_response(transcript)

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

    def _stream_response(self, user_text: str):
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
        """
        # Queue for sentences waiting to be spoken
        tts_queue: queue.Queue[str | None] = queue.Queue()
        full_response_parts = []

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
                if self._interrupted:
                    logger.info("Barge-in detected, stopping response")
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
            # Signal TTS worker to stop
            tts_queue.put(None)
            tts_thread.join(timeout=30)

        full_response = " ".join(full_response_parts)
        if full_response:
            print(f"  🌴 California: {full_response}")

        self._interrupted = False

    def _tts_worker(self, tts_queue: queue.Queue):
        """
        Pulls sentences from tts_queue, synthesizes audio, pushes to audio_queue.
        A separate _audio_player_worker thread consumes audio_queue and plays it.
        This means synthesis of sentence N+1 overlaps with playback of sentence N.
        """
        audio_queue: queue.Queue[tuple[np.ndarray, int] | None] = queue.Queue(maxsize=2)

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

                if self._interrupted:
                    while not tts_queue.empty():
                        try:
                            tts_queue.get_nowait()
                        except queue.Empty:
                            break
                    break

                try:
                    audio_data, sample_rate = self.tts.synthesize(sentence)
                    if len(audio_data) > 0:
                        audio_queue.put((audio_data, sample_rate))
                except Exception as e:
                    logger.error(f"TTS synthesis error: {e}")

        finally:
            # Signal player to stop and wait for it to finish playing
            audio_queue.put(None)
            player_thread.join(timeout=30)

    def _audio_player_worker(self, audio_queue: queue.Queue):
        """
        Pulls (audio_data, sample_rate) from audio_queue and plays them back-to-back.
        Blocks on each playback so order is preserved.
        Runs until it receives None.
        """
        while True:
            item = audio_queue.get()

            if item is None:
                break

            if self._interrupted:
                # Drain and exit
                while not audio_queue.empty():
                    try:
                        audio_queue.get_nowait()
                    except queue.Empty:
                        break
                break

            audio_data, sample_rate = item
            try:
                self.audio.play_audio(audio_data, sample_rate, blocking=True)
            except Exception as e:
                logger.error(f"TTS playback error: {e}")


    def _handle_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch tool calls from the LLM."""
        if tool_name == "control_tv":
            return _dispatch_tv(
                tool_input,
                self.media_service,
                self.stremio_service,
                self.surfshark_service,
                self.config.get("youtube_playlists", {}),
            )
        if tool_name == "control_lights":
            return _dispatch_lights(tool_input, self.govee_service)
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

        # Stop / shut up
        if lower in ("stop", "shut up", "be quiet", "cancel"):
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
