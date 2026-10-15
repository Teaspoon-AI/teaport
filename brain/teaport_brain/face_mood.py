#
# face_mood.py -- the face's mood from the words of a reply (VADER), for a reply the
# model gave no mood tag (mood_tag.py).
#
# The OLED avatar used to do this itself, from a {"text"} event. It now takes the mood
# it is told ({"mood": NAME}); this is the scoring it dropped. No Pipecat here: the
# local audio bridge imports it and stays small. The scores and thresholds are the
# avatar's (oled_face.py score_to_mood / LazyVader), except that a negative text shows sad.
#
import os
import re


def score_to_mood(c, text=""):
    """A -1..1 sentiment score + light punctuation cue -> one of the eye moods. Words of
    the assistant's own reply that score negative are sympathy ("sorry for your loss"),
    not anger: they show sad. Only a tag from the model shows angry."""
    if c >= 0.4:
        return "happy"
    if c <= -0.4:
        return "sad"
    return "curious" if text.strip().endswith("?") else "neutral"


LEAD_WORDS = 4
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
# The typographic marks a model writes, as the plain ones VADER's lexicon knows
# ("don’t" would otherwise miss its negation list, and load the emoji table).
_PLAIN = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                        "\u2013": "-", "\u2014": "-", "\u2026": "..."})


def lead_words(text: str, n: int = LEAD_WORDS) -> list:
    """The first `n` words of a reply, lowercase and without punctuation: how a mood the
    brain tagged is matched to the reply it was meant for (the face hears that reply's
    words, with no reply id to go by)."""
    return _WORD.findall(text.translate(_PLAIN).lower())[:n]


class LazyVader:
    """VADER, loaded by the first text that needs it. Speech-to-text has no emoji, so it
    loads VADER's word lexicon only, leaving out the emoji table (a third of what VADER
    holds); the whole analyzer loads, for the same scores, the first time a text has
    anything beyond ASCII."""

    def __init__(self):
        self._words = self._full = None

    @staticmethod
    def _words_only():
        import vaderSentiment.vaderSentiment as v

        class Words(v.SentimentIntensityAnalyzer):
            def __init__(self):   # (the parent's, minus the emoji table and its copies)
                self.lexicon = {}
                with open(os.path.join(os.path.dirname(v.__file__), "vader_lexicon.txt"),
                          encoding="utf-8") as f:
                    for line in f:
                        word, mean = line.rstrip("\n").split("\t")[:2]
                        self.lexicon[word] = float(mean)
                self.emojis = {}
        return Words()

    def polarity_scores(self, text):
        if text.isascii():
            if self._full is not None:
                return self._full.polarity_scores(text)
            if self._words is None:
                self._words = self._words_only()
            return self._words.polarity_scores(text)
        if self._full is None:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            self._full, self._words = SentimentIntensityAnalyzer(), None
        return self._full.polarity_scores(text)

    def mood(self, text):
        """The mood the words of `text` show."""
        text = text.translate(_PLAIN)
        return score_to_mood(self.polarity_scores(text)["compound"], text)
