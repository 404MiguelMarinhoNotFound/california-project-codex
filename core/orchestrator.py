"""
Orchestrator — The brain of Project California.

State machine that coordinates all components:
  IDLE → (wake word) → LISTENING → (silence) → PROCESSING → SPEAKING → IDLE

The key insight: the PROCESSING → SPEAKING transition is STREAMED.
LLM tokens flow through sentence chunker into TTS, so the user hears
the first sentence while the LLM is still generating.
"""

import time
import logging
import threading
import queue
import numpy as np

from core.audio_pipeline import AudioPipeline
from core.wake_word import WakeWordDetector
from core.vad import VAD
from services.stt import STTService
from services.llm import LLMService
from services.tts import TTSService
from services.sentence_chunker import chunk_sentences
from services.media_service import MediaService
from services.stremio_service import AUTOPLAY_FALLBACK_LINE, StremioService
from services.surfshark_service import SurfsharkService
from services.youtube_playlist_resolver import resolve_playlist_choice
from hardware.led_controller import LEDController

logger = logging.getLogger(__name__)

ROUTED_ACTIONS = {
    "youtube_playlist": ("youtube", "albania"),
    "youtube_search": ("youtube", "albania"),
    "stremio_play": ("stremio", "quick_connect"),
    "stremio_continue": ("stremio", "quick_connect"),
}


def _route_target_for_action(
    action: str,
    params: dict,
    route_by_app: dict | None = None,
) -> tuple[str | None, str | None]:
    route_by_app = route_by_app or {
        "youtube": "albania",
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


def _vpn_warning_suffix(target_country: str | None) -> str:
    normalized = (target_country or "").strip().lower()
    if normalized in {"quick_connect", "quick connect", "fastest", "fastest location"}:
        country_label = "Quick Connect"
    else:
        country_label = (target_country or "the right country").replace("_", " ").title()
    return f" but I couldn't confirm Surfshark was on {country_label}."


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
    target_app, target_country = _route_target_for_action(action, params, route_by_app)
    if target_app and routing_enabled:
        is_foreground = media_svc.is_app_foreground(target_app)
        logger.info(
            "VPN preflight for action=%s target_app=%s target_country=%s already_foreground=%s",
            action,
            target_app,
            target_country,
            is_foreground,
        )
        if not is_foreground:
            vpn_result = surfshark_svc.ensure_country(target_country)
            logger.info(
                "VPN preflight result for %s: success=%s switched=%s current_country=%s message=%s",
                target_app,
                vpn_result.success,
                vpn_result.switched,
                vpn_result.current_country,
                vpn_result.message,
            )
            if not vpn_result.success:
                route_warning = _vpn_warning_suffix(target_country)
            if target_app == "youtube":
                stopped = media_svc.force_stop_app("youtube")
                logger.info("Post-VPN YouTube force-stop result: %s", stopped)
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
        entry = stremio_svc.get_progress(title, refresh_if_stale=True)
        if not entry:
            return f"I couldn't find continue progress for {title}. Try syncing your Stremio library first."

        result = stremio_svc.play(
            title=entry.get("title", title),
            media_type=entry.get("type"),
            allow_unknown_source=bool(params.get("allow_unknown_source", False)),
        )
        if result.requires_confirmation:
            return result.message or AUTOPLAY_FALLBACK_LINE
        if result.success:
            response = f"Continuing {entry.get('title', title)}."
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
        playlist_id = (params.get("playlist_id") or "").strip()
        playlist_name = (params.get("playlist_name") or "").strip()
        matched_key = None

        if not playlist_id:
            matched_key, playlist_id = resolve_playlist_choice(playlist_name, youtube_playlists)

        if not playlist_id:
            fallback_name = playlist_name or "that"
            return f"I don't have a {fallback_name} playlist saved. Want me to search YouTube for it?"

        ok = media_svc.youtube_playlist(playlist_id)
        if not ok:
            return "I couldn't open that YouTube playlist right now."
        if matched_key:
            response = f"Opening your {matched_key} playlist on YouTube."
            return _append_route_warning(response, route_warning)
        response = "Opening that YouTube playlist."
        return _append_route_warning(response, route_warning)

    elif action == "youtube_search":
        query = (params.get("query") or "").strip()
        if not query:
            return "Tell me what to search for on YouTube."
        ok = media_svc.youtube_search(query)
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


class Orchestrator:
    def __init__(self, config: dict):
        self.config = config

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

        # Register tool handler so LLM can dispatch control_tv
        self.llm.tool_handler = self._handle_tool_call

        # State
        self._running = False
        self._interrupted = False  # Barge-in flag

        logger.info("All components initialized successfully")

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

        # Feed to wake word detector
        if self.wake_word.process_audio(audio_chunk):
            # Wake word detected!
            self._handle_activation(mic_stream)

    def _handle_activation(self, mic_stream):
        """
        Handle a wake word activation:
        1. Play chime
        2. Record user speech
        3. Transcribe
        4. Query LLM (streaming)
        5. Speak response (streaming)
        """
        logger.info("--- Wake word activated ---")

        # Play activation chime
        self.audio.play_activation_sound()

        # --- LISTENING: Record until silence ---
        self.leds.set_state("listening")
        audio_data = self._record_speech(mic_stream)

        if audio_data is None or len(audio_data) == 0:
            logger.info("No speech recorded, returning to idle")
            return

        # --- PROCESSING: STT → LLM ---
        self.leds.set_state("thinking")

        # Convert to WAV and transcribe
        wav_bytes = self.audio.numpy_to_wav_bytes(audio_data)
        transcript = self.stt.transcribe(wav_bytes)

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

    def _record_speech(self, mic_stream) -> np.ndarray | None:
        """
        Record audio until VAD detects silence.
        Returns numpy array of recorded audio (int16), or None if too short.
        """
        self.vad.start_recording()
        chunks = []

        while True:
            audio_bytes, overflowed = mic_stream.read(self.audio.chunk_samples)
            audio_chunk = self.audio.bytes_to_numpy(audio_bytes)
            chunks.append(audio_chunk)

            should_stop, reason = self.vad.should_stop_recording(audio_chunk)
            if should_stop:
                logger.info(f"Recording stopped: {reason}")
                break

        if not chunks:
            return None

        audio_data = np.concatenate(chunks)
        duration = len(audio_data) / self.audio.sample_rate
        logger.info(f"Recorded {duration:.1f}s of audio")

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
