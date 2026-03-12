"""
LED Controller — Visual state feedback.

On Pi with ReSpeaker HAT: Uses pixel_ring for LED animations.
On laptop/dev: Prints state changes to console with color indicators.
"""

import logging

logger = logging.getLogger(__name__)

# ANSI color codes for terminal feedback
COLORS = {
    "idle": "\033[90m●\033[0m",         # Gray
    "listening": "\033[94m●\033[0m",    # Blue
    "thinking": "\033[95m●\033[0m",     # Purple
    "speaking": "\033[92m●\033[0m",     # Green
    "error": "\033[91m●\033[0m",        # Red
}

STATE_LABELS = {
    "idle": "Idle — Listening for wake word...",
    "listening": "Listening — Speak now...",
    "thinking": "Thinking...",
    "speaking": "Speaking...",
    "error": "Error",
}


class LEDController:
    def __init__(self, config: dict):
        led_cfg = config.get("leds", {})
        self.enabled = led_cfg.get("enabled", False)
        self.brightness = led_cfg.get("brightness", 0.5)
        self._current_state = None

        # Try to load pixel_ring (Pi with ReSpeaker)
        self._pixel_ring = None
        if self.enabled:
            try:
                from pixel_ring import pixel_ring
                pixel_ring.set_brightness(int(self.brightness * 100))
                self._pixel_ring = pixel_ring
                logger.info("LED controller: pixel_ring initialized")
            except (ImportError, IOError):
                logger.info("LED controller: pixel_ring not available, using console output")
                self.enabled = False

    def set_state(self, state: str):
        """
        Set the visual state.
        States: idle, listening, thinking, speaking, error
        """
        if state == self._current_state:
            return

        self._current_state = state

        # Console feedback (always)
        indicator = COLORS.get(state, "○")
        label = STATE_LABELS.get(state, state)
        print(f"\r  {indicator} {label}        ", end="", flush=True)

        # Hardware LEDs (Pi only)
        if self._pixel_ring:
            try:
                match state:
                    case "idle":
                        self._pixel_ring.off()
                    case "listening":
                        self._pixel_ring.listen()
                    case "thinking":
                        self._pixel_ring.think()
                    case "speaking":
                        self._pixel_ring.speak()
                    case "error":
                        self._pixel_ring.set_color(255, 0, 0)
            except Exception as e:
                logger.debug(f"LED error: {e}")

    def off(self):
        """Turn off all LEDs."""
        self._current_state = None
        if self._pixel_ring:
            self._pixel_ring.off()
