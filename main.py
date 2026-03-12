#!/usr/bin/env python3
"""
🌴 Project California — DIY Voice Assistant

Usage:
    python main.py              # Normal mode
    python main.py --debug      # Debug logging
    python main.py --test-mic   # Test microphone
    python main.py --test-tts   # Test TTS output
    python main.py --test-stt   # Record and transcribe
    python main.py --test-llm   # Test LLM with text input
"""

# ============================================================
# CONFIGURATION & API KEYS
# ============================================================
# All API keys are loaded from .env file (copy .env.example to .env)
# All tunables are in config.yaml
# ============================================================

import os
import sys
import logging
import argparse

import yaml
from dotenv import load_dotenv


def setup_logging(debug: bool = False):
    """Configure logging."""
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openwakeword").setLevel(logging.WARNING)


def load_config(path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return config


def test_microphone(config: dict):
    """Test microphone input — record 3 seconds and report levels."""
    from core.audio_pipeline import AudioPipeline
    import numpy as np

    print("\n🎤 Microphone Test")
    print("  Recording 3 seconds of audio...")
    print("  Speak or make noise to see levels.\n")

    audio = AudioPipeline(config)
    stream = audio.create_mic_stream()
    stream.start()

    chunks = []
    num_chunks = int(3.0 / (config["audio"]["chunk_duration_ms"] / 1000))

    for i in range(num_chunks):
        data, _ = stream.read(audio.chunk_samples)
        chunk = audio.bytes_to_numpy(data)
        chunks.append(chunk)

        rms = np.sqrt(np.mean(chunk.astype(np.float32) ** 2))
        bar_len = int(rms / 100)
        bar = "█" * min(bar_len, 50)
        print(f"\r  RMS: {rms:6.0f} |{bar:<50}|", end="", flush=True)

    stream.stop()
    stream.close()

    all_audio = np.concatenate(chunks)
    rms_avg = np.sqrt(np.mean(all_audio.astype(np.float32) ** 2))
    rms_max = np.sqrt(np.max(all_audio.astype(np.float32) ** 2))

    print(f"\n\n  Average RMS: {rms_avg:.0f}")
    print(f"  Peak RMS:    {rms_max:.0f}")
    print(f"\n  💡 Set vad.energy_threshold in config.yaml to ~{int(rms_avg * 1.5)}")
    print(f"     (1.5x your ambient noise level)\n")


def test_tts(config: dict):
    """Test TTS output."""
    from services.tts import TTSService
    from core.audio_pipeline import AudioPipeline

    print(f"\n🔊 TTS Test (provider: {config['tts']['provider']})")

    tts = TTSService(config)
    audio_pipeline = AudioPipeline(config)

    test_text = "Hello! I'm California, your voice assistant. How can I help you today?"
    print(f"  Saying: '{test_text}'")

    audio, sr = tts.synthesize(test_text)
    print(f"  Generated {len(audio)/sr:.1f}s of audio at {sr}Hz")

    audio_pipeline.play_audio(audio, sr, blocking=True)
    print("  ✅ Done!\n")


def test_stt(config: dict):
    """Record audio and transcribe it."""
    from core.audio_pipeline import AudioPipeline
    from core.vad import VAD
    from services.stt import STTService
    import numpy as np

    print(f"\n🎤→📝 STT Test (provider: {config['stt']['provider']})")
    print("  Speak after the beep, I'll transcribe when you stop.\n")

    audio_pipeline = AudioPipeline(config)
    vad = VAD(config)
    stt = STTService(config)

    # Play chime
    audio_pipeline.play_activation_sound()

    # Record
    print("  🔴 Recording... (speak now)")
    stream = audio_pipeline.create_mic_stream()
    stream.start()
    vad.start_recording()

    chunks = []
    while True:
        data, _ = stream.read(audio_pipeline.chunk_samples)
        chunk = audio_pipeline.bytes_to_numpy(data)
        chunks.append(chunk)

        should_stop, reason = vad.should_stop_recording(chunk)
        if should_stop:
            break

    stream.stop()
    stream.close()

    audio_data = np.concatenate(chunks)
    duration = len(audio_data) / audio_pipeline.sample_rate
    print(f"  Recorded {duration:.1f}s")

    # Transcribe
    print("  Transcribing...")
    wav_bytes = audio_pipeline.numpy_to_wav_bytes(audio_data)
    text = stt.transcribe(wav_bytes)
    print(f"\n  📝 Transcription: '{text}'\n")


def test_llm(config: dict):
    """Test LLM with typed text input."""
    from services.llm import LLMService

    print(f"\n🧠 LLM Test (provider: {config['llm']['provider']})")
    print("  Type messages to chat. Press Ctrl+C to exit.\n")

    llm = LLMService(config)

    while True:
        try:
            user_input = input("  You: ").strip()
            if not user_input:
                continue

            print("  California: ", end="", flush=True)
            for token in llm.stream_response(user_input):
                print(token, end="", flush=True)
            print("\n")

        except KeyboardInterrupt:
            print("\n  Done!\n")
            break


def test_full_pipeline(config: dict):
    """Test the full pipeline without wake word (press Enter to activate)."""
    from core.audio_pipeline import AudioPipeline
    from core.vad import VAD
    from services.stt import STTService
    from services.llm import LLMService
    from services.tts import TTSService
    from services.sentence_chunker import chunk_sentences
    from hardware.led_controller import LEDController
    import numpy as np
    import threading
    import queue as q

    print("\n🌴 Full Pipeline Test (no wake word)")
    print("  Press Enter to start recording, Ctrl+C to exit.\n")

    audio_pipeline = AudioPipeline(config)
    vad = VAD(config)
    stt = STTService(config)
    llm = LLMService(config)
    tts = TTSService(config)
    leds = LEDController(config)

    while True:
        try:
            input("  Press Enter to speak...")

            # Record
            leds.set_state("listening")
            audio_pipeline.play_activation_sound()

            stream = audio_pipeline.create_mic_stream()
            stream.start()
            vad.start_recording()

            chunks = []
            while True:
                data, _ = stream.read(audio_pipeline.chunk_samples)
                chunk = audio_pipeline.bytes_to_numpy(data)
                chunks.append(chunk)
                should_stop, _ = vad.should_stop_recording(chunk)
                if should_stop:
                    break

            stream.stop()
            stream.close()

            audio_data = np.concatenate(chunks)

            # Transcribe
            leds.set_state("thinking")
            wav_bytes = audio_pipeline.numpy_to_wav_bytes(audio_data)
            transcript = stt.transcribe(wav_bytes)
            print(f"\n  👤 You: {transcript}")

            if not transcript.strip():
                continue

            # LLM → TTS streaming
            tts_queue: q.Queue = q.Queue()

            def tts_worker():
                while True:
                    sentence = tts_queue.get()
                    if sentence is None:
                        break
                    audio, sr = tts.synthesize(sentence)
                    if len(audio) > 0:
                        audio_pipeline.play_audio(audio, sr, blocking=True)

            tts_thread = threading.Thread(target=tts_worker, daemon=True)
            tts_thread.start()

            print("  🌴 California: ", end="", flush=True)
            full_resp = []
            first = True
            for sentence in chunk_sentences(llm.stream_response(transcript)):
                if first:
                    leds.set_state("speaking")
                    first = False
                full_resp.append(sentence)
                tts_queue.put(sentence)

            tts_queue.put(None)
            tts_thread.join(timeout=30)

            print(" ".join(full_resp))
            leds.set_state("idle")
            print()

        except KeyboardInterrupt:
            print("\n  Done!\n")
            leds.off()
            break


def main():
    parser = argparse.ArgumentParser(description="🌴 Project California — Voice Assistant")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--test-mic", action="store_true", help="Test microphone levels")
    parser.add_argument("--test-tts", action="store_true", help="Test text-to-speech")
    parser.add_argument("--test-stt", action="store_true", help="Test speech-to-text")
    parser.add_argument("--test-llm", action="store_true", help="Test LLM chat (text input)")
    parser.add_argument("--test-pipeline", action="store_true",
                        help="Test full pipeline without wake word (press Enter to activate)")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    args = parser.parse_args()

    # Load environment variables
    load_dotenv()

    # Setup logging
    setup_logging(args.debug)

    # Load config
    config = load_config(args.config)

    # Run requested mode
    if args.test_mic:
        test_microphone(config)
    elif args.test_tts:
        test_tts(config)
    elif args.test_stt:
        test_stt(config)
    elif args.test_llm:
        test_llm(config)
    elif args.test_pipeline:
        test_full_pipeline(config)
    else:
        # Full assistant mode
        from core.orchestrator import Orchestrator
        orchestrator = Orchestrator(config)
        orchestrator.run()


if __name__ == "__main__":
    main()
