#
# stt_harness.py — the shared stand-in for driving TeaportSTTService from a test.
#
# Build the service with its REAL constructor (url="ws://127.0.0.1:1/none" is never
# connected to) and install WireRecorder as its websocket: what a test asserts on is
# then what run_stt put on the wire, not a transform re-applied by the test. The
# alternative -- __new__ plus hand-set attributes -- has to mirror every field the
# constructor gains, and a miss surfaces as an AttributeError inside a timer task that
# asyncio swallows: the stub-drift failure test_stranded_segment_commit.py records three
# prior instances of.
#
import base64
import json


class WireRecorder:
    """A websocket that records what the service sends, decoded, instead of sending."""

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass

    def types(self):
        return [m["type"] for m in self.sent]

    def commits(self):
        return [m for m in self.sent if m["type"] == "input_audio_buffer.commit"]

    def audio(self) -> bytes:
        """Every appended chunk, decoded and concatenated in send order."""
        return b"".join(base64.b64decode(m["audio"])
                        for m in self.sent if m["type"] == "input_audio_buffer.append")
