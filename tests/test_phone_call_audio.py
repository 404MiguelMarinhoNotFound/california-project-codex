"""PhoneCallAudio: the call's ears and mouth over the helper's socket. No real phone."""

import socket
import threading
import time
import unittest

from services.phone_adb import ACTIVE, ALERTING, DIALING
from services.phone_call_audio import PLAY_RATE, REC_RATE, PhoneCallAudio, _LEAD_S


class FakeProc:
    def __init__(self):
        self.stdout = _Lines(["LISTENING 27183\n"])
        self.stderr = iter([])
        self.killed = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


class _Lines:
    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0) if self.lines else ""


class FakePhone:
    port = 27183

    def __init__(self, states, mute=False):
        self.states = list(states)
        self.mute_phone_mic = mute
        self.helper_args = []
        self.helper_at_state = []
        self.forwarded, self.unforwarded = [], []

    def call_state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def helper(self, *args):
        self.helper_args.append(args)
        self.helper_at_state.append(self.states[0])
        return FakeProc()

    def forward(self, port):
        self.forwarded.append(port)

    def unforward(self, port):
        self.unforwarded.append(port)


class _Base(unittest.TestCase):
    def setUp(self):
        self.ours, self.phone_end = socket.socketpair()
        self.addCleanup(self.phone_end.close)

    def bridge(self, states=(ACTIVE,), mute=False):
        self.phone = FakePhone(states, mute=mute)
        bridge = PhoneCallAudio(self.phone, connect=lambda addr, timeout=None: self.ours, poll_s=0.01)
        self.addCleanup(bridge.stop)
        return bridge

    def wait_connected(self, bridge, within=2.0):
        deadline = time.monotonic() + within
        while not bridge.answered() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(bridge.answered())

    def drain(self, seconds):
        """Bytes the phone end received within `seconds`."""
        self.phone_end.settimeout(0.02)
        got, end = b"", time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                got += self.phone_end.recv(65536)
            except (socket.timeout, BlockingIOError):
                pass
        return got


class ConnectTests(_Base):
    def test_the_helper_starts_only_once_the_call_is_answered(self):
        bridge = self.bridge(states=(DIALING, ALERTING, ALERTING, ACTIVE))
        bridge.start(lambda pcm: None)
        self.wait_connected(bridge)
        self.assertEqual(len(self.phone.helper_args), 1)
        self.assertEqual(self.phone.helper_at_state, [ACTIVE])
        self.assertEqual(self.phone.helper_args[0], ("bridge", "27183", str(REC_RATE), str(PLAY_RATE)))
        self.assertEqual(self.phone.forwarded, [27183])

    def test_answered_is_false_until_connected(self):
        bridge = self.bridge(states=(DIALING,))
        bridge.start(lambda pcm: None)
        time.sleep(0.1)
        self.assertFalse(bridge.answered())

    def test_mutemic_is_passed_when_configured(self):
        bridge = self.bridge(mute=True)
        bridge.start(lambda pcm: None)
        self.wait_connected(bridge)
        self.assertEqual(self.phone.helper_args[0][-1], "mutemic")


class EarsTests(_Base):
    def test_downlink_blocks_reach_the_agent_in_20ms_pieces(self):
        blocks = []
        bridge = self.bridge()
        bridge.start(blocks.append)
        self.wait_connected(bridge)
        loud = (b"\x00\x40" * 1000)  # 2000 bytes of a loud signal
        self.phone_end.sendall(loud)
        deadline = time.monotonic() + 2
        while len(blocks) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([len(b) for b in blocks], [640, 640, 640])
        self.assertEqual(bridge.blocks, 3)
        self.assertGreater(bridge.peak, 0.3)


class MouthTests(_Base):
    def test_her_voice_is_paced_to_real_time(self):
        bridge = self.bridge()
        bridge.start(lambda pcm: None)
        self.wait_connected(bridge)
        bridge.write(b"\x01\x00" * PLAY_RATE)  # 1 s of her voice
        got = self.drain(0.3)
        self.assertLessEqual(len(got), int((0.3 + _LEAD_S + 0.08) * PLAY_RATE * 2))
        self.assertGreater(len(got), int(0.15 * PLAY_RATE * 2))

    def test_flush_drops_what_has_not_been_sent(self):
        bridge = self.bridge()
        bridge.start(lambda pcm: None)
        self.wait_connected(bridge)
        bridge.write(b"\x01\x00" * PLAY_RATE * 2)  # 2 s
        time.sleep(0.1)
        bridge.flush()
        got = self.drain(0.6)
        self.assertLess(len(got), int(0.5 * PLAY_RATE * 2))
        self.assertTrue(bridge.idle())


class StopTests(_Base):
    def test_stop_unblocks_a_reader_and_removes_the_forward(self):
        bridge = self.bridge()
        bridge.start(lambda pcm: None)
        self.wait_connected(bridge)
        started = time.monotonic()
        bridge.stop()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.phone.unforwarded, [27183])
        bridge.stop()
        self.assertEqual(self.phone.unforwarded, [27183])

    def test_stop_before_the_answer_launches_nothing(self):
        bridge = self.bridge(states=(DIALING,))
        bridge.start(lambda pcm: None)
        time.sleep(0.05)
        bridge.stop()
        time.sleep(0.05)
        self.assertEqual(self.phone.helper_args, [])


class FacesTests(_Base):
    def test_both_faces_share_one_bridge_and_stop_it_once(self):
        bridge = self.bridge()
        capture, speaker = bridge.capture(), bridge.speaker()
        capture.start(lambda pcm: None)
        speaker.start()
        self.wait_connected(bridge)
        speaker.write(b"\x00\x00" * 10)
        capture.stop()
        speaker.stop()
        self.assertEqual(self.phone.unforwarded, [27183])
        self.assertEqual(capture.source, "phone downlink")


if __name__ == "__main__":
    unittest.main()
