#
# Unit test for HeardContextCorrector against the context shapes pipecat ACTUALLY
# produces on a barge-in. Key reality (see heard_context.py): the aggregator
# commits the TTS-spoken text, not the raw LLM text, and on an early barge-in the
# spoken message may be PARTIAL or ABSENT. So the corrector matches by position,
# overwriting the cut turn's spoken message with the ledger's heard prefix, or
# inserting one if pipecat committed none. Tool results must survive.
#
# Run: python test_heard_truncate.py
#

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from teaport_brain.heard_context import HeardContextCorrector  # noqa: E402
from teaport_brain.transcript_ledger import Utterance  # noqa: E402

HN_FULL = "The top three are Nobody ever gets credit, then Claude Fable, then Homebrew."
HN_HEARD = "The top three are Nobody ever gets credit,"  # heard prefix (trailing comma cleaned off)
ROME_FULL = "Ancient Rome began as a small city-state, grew into a vast empire."
ROME_HEARD = "Ancient Rome began as a small city-state,"


class FakeContext:
    def __init__(self, messages):
        self._m = messages

    def get_messages(self, *a, **k):
        return self._m

    def set_messages(self, m):
        self._m[:] = m

    def add_message(self, m):
        self._m.append(m)


def U(text, heard_text, frac=0.4, interrupted=True):
    # Real Utterance (not a stub): the corrector relies on its cut_short /
    # unheard_tail members, so the test must exercise the actual dataclass.
    return Utterance(speaker="assistant", text=text, t_start=0.0, t_end=1.0,
                     interrupted=interrupted, heard_fraction=frac,
                     heard_text=heard_text)


def run(ledger_events, msgs, mode="truncate", mark=0):
    c = HeardContextCorrector.__new__(HeardContextCorrector)
    c._ledger, c._context, c._mode, c._done = (
        SimpleNamespace(events=ledger_events), FakeContext(msgs), mode, 0)
    # How long the context was at the previous reconcile; anything older is settled.
    c._mark = mark
    c._reconcile()
    return c._context._m


def hn_tool(spoken=None, triggers=("What was the last one you said?",)):
    m = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "What's on Hacker News?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_hacker_news_top", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "1) Nobody… 2) Claude Fable… 3) Homebrew…"},
    ]
    if spoken is not None:
        m.append({"role": "assistant", "content": spoken})
    m += [{"role": "user", "content": t} for t in triggers]
    return m


def asst_text(msgs):
    return [m for m in msgs if m["role"] == "assistant"
            and isinstance(m.get("content"), str) and m["content"].strip()]


def test_tool_spoken_absent_inserts():
    # synthesis cut before any TTS text -> pipecat committed NO spoken message
    out = run([U(HN_FULL, HN_HEARD)], hn_tool(spoken=None))
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert out[-2]["content"] == "The top three are Nobody ever gets credit"  # inserted, cleaned
    assert "Homebrew" in out[3]["content"], "tool result must survive"
    print("  PASS tool/absent -> inserted heard reply after tool result")


def test_tool_partial_overwritten():
    # partial TTS text committed -> overwrite it with the accurate heard prefix
    out = run([U(HN_FULL, HN_HEARD)], hn_tool(spoken="The top three are Nobody ever gets credit, then Claude"))
    assert out[-2]["content"] == "The top three are Nobody ever gets credit"
    assert "Claude" not in out[-2]["content"]
    assert "Homebrew" in out[3]["content"]
    print("  PASS tool/partial -> overwrote spoken with heard")


def test_multi_fragment_trigger():
    # fragmented STT -> two trailing user turns; still target the right message
    out = run([U(HN_FULL, HN_HEARD)],
              hn_tool(spoken="The top three are Nobody ever gets credit, then Claude",
                      triggers=("wait", "what was the last one")))
    assert out[-3]["content"] == "The top three are Nobody ever gets credit"
    assert out[-1]["role"] == "user" and out[-2]["role"] == "user"
    print("  PASS multi-fragment trigger -> overwrote correct message")


def test_prose_overwrite():
    out = run([U(ROME_FULL, ROME_HEARD)],
              [{"role": "system", "content": "s"}, {"role": "user", "content": "rome?"},
               {"role": "assistant", "content": ROME_FULL}, {"role": "user", "content": "what?"}])
    assert out[2]["content"] == "Ancient Rome began as a small city-state"
    assert "empire" not in out[2]["content"]
    print("  PASS prose -> overwrote to heard prefix")


def test_nothing_heard_removes():
    out = run([U(ROME_FULL, "", frac=0.0)],
              [{"role": "system", "content": "s"}, {"role": "user", "content": "rome?"},
               {"role": "assistant", "content": ROME_FULL}, {"role": "user", "content": "what?"}])
    assert not asst_text(out), "unheard reply should be removed"
    print("  PASS nothing-heard -> removed reply")


def test_note_mode_additive():
    out = run([U(ROME_FULL, ROME_HEARD)],
              [{"role": "system", "content": "s"}, {"role": "user", "content": "rome?"},
               {"role": "assistant", "content": ROME_FULL}, {"role": "user", "content": "what?"}],
              mode="note")
    assert any(m["role"] == "assistant" and m.get("content") == ROME_FULL for m in out), "full reply kept"
    assert out[-1]["role"] == "system" and "did not hear" in out[-1]["content"]
    print("  PASS note mode -> additive note, full reply intact")


def test_fully_heard_untouched():
    # heard_fraction >= 0.99 -> not a cut; corrector ignores it
    before = [{"role": "system", "content": "s"}, {"role": "assistant", "content": ROME_FULL}]
    out = run([U(ROME_FULL, ROME_FULL, frac=1.0, interrupted=False)], list(before))
    assert out == before
    print("  PASS fully-heard -> untouched")


def test_midword_rounds_to_complete_item():
    # cut landed mid "cold-pressed olive oil" -> answer the last COMPLETE item
    full = "It has fresh basil, aged parmesan, cold-pressed olive oil, and nutmeg."
    heard = "It has fresh basil, aged parmesan, cold-pressed"  # mid-item
    out = run([U(full, heard)],
              [{"role": "system", "content": "s"}, {"role": "user", "content": "q"},
               {"role": "assistant", "content": "It has fresh basil, aged parmesan, cold"},
               {"role": "user", "content": "what was the last one?"}])
    assert out[2]["content"] == "It has fresh basil, aged parmesan", out[2]["content"]
    print("  PASS mid-word -> rounded to last complete item ('aged parmesan')")


def test_a_settled_reply_is_never_removed():
    """The cut turn committed nothing, so the walk must not reach back a turn.

    When the user hears NONE of a reply, pipecat commits no spoken text for it at all
    (it appends the TTSTextFrame, and none was pushed). The tail is then the previous
    exchange, and an unbounded walk removes a reply the user did hear.

    Live 2026-08-26 08:17: a background consult's answer was delivered and heard in
    full; the user said "Thanks, Kettlebot."; the one-line reply to THAT was barged
    over at 0% heard; reconciling that cut deleted the delivery. With its own answer
    gone from the context the model recited the whole shop list again — the user saw
    the reply repeat twice.
    """
    delivered = "About that pastry-shop list: Upper Crust Bakery and La Patisserie."
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "any pastry shops on Burnet?"},
            {"role": "assistant", "content": delivered},   # heard in full
            {"role": "user", "content": "Thanks, Kettlebot."},
            {"role": "user", "content": "That's useful."}]
    # The context was 4 long when the "Thanks, Kettlebot." turn started; the reply to
    # it ("You're welcome...") was cut before a word of it was spoken and committed
    # nothing, so only "That's useful." has been added since.
    out = run([U("You're welcome! Let me know if you need anything else.", "", frac=0.0)],
              msgs, mark=4)
    kept = [m["content"] for m in asst_text(out)]
    assert kept == [delivered], (
        f"the settled, fully-heard reply was altered or removed: {kept!r} — "
        f"the model will now repeat it")
    print("  PASS settled reply -> untouched by a later 0%-heard cut")


def test_no_bot_turn_safe():
    # a cut event but no bot turn in context -> no-op, no crash
    out = run([U(ROME_FULL, ROME_HEARD)],
              [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
    assert [m["role"] for m in out] == ["system", "user"]
    print("  PASS no-bot-turn -> safe no-op")


if __name__ == "__main__":
    for fn in [test_tool_spoken_absent_inserts, test_tool_partial_overwritten,
               test_multi_fragment_trigger, test_prose_overwrite,
               test_nothing_heard_removes, test_note_mode_additive,
               test_fully_heard_untouched, test_midword_rounds_to_complete_item,
               test_no_bot_turn_safe, test_a_settled_reply_is_never_removed]:
        fn()
    print("ALL PASS")
