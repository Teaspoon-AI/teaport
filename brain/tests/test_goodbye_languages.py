#
# #320: the room's goodbyes in every language -- the model's goodbye that the #221
# backstop sends the room to sleep on (goodbye.goodbye_kind), and the caller's own
# whole-turn goodbye, which the sleep phrase now hears with the same matcher
# (goodbye.SleepPhrase, #221's option 1).
#
# Each language's words are its catalog's "pattern" entries (goodbye.PATTERNS): English,
# Spanish and Italian have their own; every other catalog carries the English pattern,
# so its sessions hear English goodbyes alone. Per language: goodbyes that count, and
# the controls that must not -- a question, a quoted goodbye, a goodbye inside a longer
# answer, "good night" in the middle of a sentence. All examples are invented.
#
# Run: python test_goodbye_languages.py   (or via the suite)
#
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_goodbye import _Owed, _Voice, until  # noqa: E402  (sets the env it needs)

from teaport_brain import goodbye, i18n, wifi_voice  # noqa: E402
from teaport_brain.goodbye import BYE, SENT, SLEEP, goodbye_kind  # noqa: E402

RELEASE = ("en", "es", "it")

# The model's replies that are only a goodbye: (text, kind).
MODEL = {
    "en": [("Good night!", BYE), ("Okay, going to sleep. Good night!", SLEEP),
           ("Bye, take care!", BYE), ("See you tomorrow, Sam.", BYE)],
    "es": [("¡Buenas noches, que descanses!", BYE), ("Que descanses.", BYE),
           ("¡Buenas noches!", BYE), ("Hasta mañana, Ana.", BYE), ("Vale, hasta luego.", BYE),
           ("¡Adiós! Cuídate mucho.", BYE), ("Dulces sueños.", BYE),
           ("Nos vemos mañana, amigo.", BYE), ("Gracias, igualmente. ¡Buenas noches!", BYE),
           ("¡Que tengas una buena noche!", BYE), ("Chao, chao.", BYE),
           ("Hasta la próxima.", BYE), ("Que duermas bien.", BYE), ("Que le vaya bien.", BYE),
           ("Cuídese, hasta pronto.", BYE), ("Buen descanso y dulces sueños.", BYE),
           ("Ciao, hasta mañana.", BYE),                    # how an STT writes "chao"
           ("Vale, me voy a dormir.", SLEEP), ("De acuerdo, dejo de escuchar. ¡Hasta luego!", SLEEP),
           ("Me alegra haberte ayudado. ¡Que descanses!", BYE)],
    "it": [("Buonanotte!", BYE), ("Buonanotte, Marco!", BYE), ("Sogni d'oro.", BYE),
           ("Va bene, a domani!", BYE), ("Ciao ciao!", BYE), ("Ciao!", BYE),
           ("Ci sentiamo domani.", BYE), ("Grazie, anche a te. Buona serata!", BYE),
           ("Arrivederci.", BYE), ("Arrivederla, buona giornata.", BYE),
           ("Dormi bene, a presto.", BYE), ("Alla prossima!", BYE), ("Riposati.", BYE),
           ("Buona notte a tutti.", BYE), ("Stammi bene!", BYE),
           ("Va bene, vado a dormire.", SLEEP), ("D'accordo, smetto di ascoltare. Buonanotte!", SLEEP),
           ("È stato un piacere. Buonanotte!", BYE)],
}

# The controls: none of these is a goodbye.
NOT_MODEL = {
    "en": ["Going to bed already?", '"Good night" in Spanish is "buenas noches".',
           "Good night! Your alarm is set for seven.", "I said good night to the kids."],
    "es": ["¿Ya te vas a dormir?", "¿Buenas noches?", "“Buenas noches” en inglés es “good night”.",
           "¡Buenas noches! Tu alarma está puesta para las siete.",
           "Hasta mañana, la reunión es a las nueve.", "Les dije buenas noches a los niños.",
           "Adiós y apaga la luz.", "Hola, buenas noches", "Vale.", "Gracias.",
           "Buenas.", "Te digo buenas noches y me voy"],
    "it": ["Vai già a dormire?", "Buonanotte?", '"Buonanotte" in inglese si dice "good night".',
           "Buonanotte! La sveglia è alle sette.", "A domani, ricordati la riunione.",
           "Ho detto buonanotte ai bambini.", "Buonanotte e spegni la luce.", "Buonasera!",
           "Salve.", "Grazie.", "Ti dico buonanotte e poi vado"],
}


def test_the_models_goodbyes_in_each_release_language():
    for lang in RELEASE:
        t = i18n.get(lang)
        wrong = [(text, goodbye_kind(text, t), kind) for text, kind in MODEL[lang]
                 if goodbye_kind(text, t) != kind]
        assert not wrong, (lang, wrong)
        taken = [text for text in NOT_MODEL[lang] if goodbye_kind(text, t) is not None]
        assert not taken, (lang, taken)


def test_a_name_beside_the_goodbye_but_no_other_clause():
    # Unpunctuated, one capitalised word or a word for anyone; after a comma, one or two.
    en, it_ = i18n.get("en"), i18n.get("it")
    for text in ("good night Sam", "good night Teaport", "good night everyone",
                 "good night, Sam Smith", "buonanotte Marco", "buonanotte, Marco Rossi",
                 "good night sleep well", "buonanotte sogni d'oro"):
        assert goodbye_kind(text, it_) == BYE, text
    for text in ("good night Alarm Set", "Good night Door Locked", "good night alarm set",
                 "buonanotte Sveglia Impostata", "good night Sam Smith"):
        assert goodbye_kind(text, it_) is None and goodbye_kind(text, en) is None, text
    es, it = i18n.get("es"), i18n.get("it")
    assert goodbye_kind("¡Buenas noches, Lucía!", es) == BYE
    assert goodbye_kind("Hasta mañana, la alarma está puesta.", es) is None
    assert goodbye_kind("Buonanotte, Giulia!", it) == BYE
    assert goodbye_kind("Buonanotte, sveglia impostata.", it) is None


def test_a_word_that_greets_too_is_a_goodbye_only_once_the_conversation_is_under_way():
    """Spanish "buenas noches" is good evening as well as good night, Italian "ciao"
    hello as well as bye: as the caller's first words they greet."""
    for lang, word in (("es", "Buenas noches."), ("it", "Ciao!")):
        t = i18n.get(lang)
        assert goodbye_kind(word, t) == BYE
        assert goodbye_kind(word, t, greets_part=False) is None
    # Beside a goodbye that only parts, it is small talk, and the whole still parts.
    assert goodbye_kind("Buenas noches, hasta mañana.", i18n.get("es"), greets_part=False) == BYE
    assert goodbye_kind("Ciao, a domani!", i18n.get("it"), greets_part=False) == BYE


# What the caller asks, and what they don't: a question, a request for a word, a request
# that the box go (whose answer is a goodbye).
ASKS = {
    "es": ["¿Cómo se dice adiós en inglés?", "Traduce buenas noches al inglés.",
           "Qué hora es", "Dime el contrario de hola.", "¿Puedes recordármelo mañana?",
           "sigues ahí", "qué tal", "me oyes"],
    "it": ["Come si dice buonanotte in inglese?", "Traduci arrivederci in inglese.",
           "Che ore sono", "Dimmi il contrario di ciao.", "Puoi ricordarmelo domani?",
           "come stai", "come va", "come faccio a spegnerti", "ci sei"],
}
ASKS_NOT = {
    "es": ["¿Puedes irte a dormir?", "¿Por qué sigues despierto?", "Bueno, buenas noches.",
           "Que descanses.", "Como quieras, hasta mañana."],
    "it": ["Puoi andare a dormire?", "Perché sei ancora sveglio?", "Va bene, buonanotte.",
           "Come vuoi, a domani."],
}


def test_what_counts_as_the_caller_asking_in_each_release_language():
    for lang in ("es", "it"):
        t = i18n.get(lang)
        assert [s for s in ASKS[lang] if not goodbye.asks(s, t)] == [], lang
        assert [s for s in ASKS_NOT[lang] if goodbye.asks(s, t)] == [], lang


# The caller's own turn: a request, or only a goodbye (whole turns only).
CALLER = {
    "en": ["Good night.", "Okay, good night!", "Goodnight", "Bye!", "Thanks, bye.",
           "Stop listening.", "Okay, go to sleep.", "I'm going to sleep now.",
           "See you tomorrow.",
           # as an STT writes them, unpunctuated (review round 1)
           "okay bye", "thanks bye", "alright good night", "good night thanks", "good night Sam"],
    "es": ["Buenas noches.", "Bueno, buenas noches.", "Hasta mañana.", "Adiós, gracias.",
           "Vale, chao.", "A dormir.", "Vete a dormir.", "Vete a la cama.", "Deja de escucharme.",
           "Me voy a dormir.", "Venga, a dormir.", "Duérmase, por favor.", "Hasta luego, Teaport.",
           "Buenas noches, que descanses.",
           "vale adiós", "bueno hasta mañana", "gracias hasta luego", "pues nada hasta mañana",
           "adiós gracias", "ciao", "buenas noches María"],
    "it": ["Buonanotte.", "Va bene, buonanotte!", "A domani.", "Ciao ciao.", "Grazie, ciao!",
           "Vai a dormire.", "Va' a dormire.", "A nanna!", "Vai a nanna.", "Smetti di ascoltare.",
           "Vado a dormire.", "Ci sentiamo domani.", "Dai, vai a letto.", "Notte, teaport.",
           "ok buonanotte", "va bene buonanotte", "grazie ciao", "buonanotte grazie",
           "va bene a domani", "buonanotte Marco"],
}
NOT_CALLER = {
    "en": ["Good night?", "How do I say good night in French?", "I said good night to the kids.",
           "Good night, and remind me at seven.", "Stop listening to me.",
           "ok bye so what's the weather tomorrow", "bye the way what's the time"],
    "es": ["¿Buenas noches?", "¿Ya te vas a dormir?", "Les dije buenas noches a los niños.",
           "Buenas noches, ¿qué tiempo hará mañana?", "Hasta mañana a las siete, pon la alarma.",
           "“Adiós” se dice goodbye.", "No te vayas a dormir.",
           "hasta mañana si quieres", "me duermo tarde siempre", "hasta luego me dijo"],
    "it": ["Buonanotte?", "Vai già a dormire?", "Ho detto buonanotte ai bambini.",
           "Buonanotte, che tempo fa domani?", "A domani alle sette, metti la sveglia.",
           "\"Ciao\" si dice hello.", "Non andare a dormire.",
           "a dopo ti chiamo", "ci vediamo al cinema", "vado a dormire tardi stasera",
           "a la prossima"],
}


def _takes(lang, text, *, first=False, book=None):
    """Whether the sleep phrase, on a fake voice in `lang`, takes `text` (a caller's turn
    after one other, or as their `first` words), and the voice."""
    async def run():
        v = _Voice(lang)
        if book is not None:
            v.sleep.gate = _Gate()
            v.sleep.owed = book
        if not first:
            await v._heard("una cosa" if lang != "en" else "one thing")
        return await v._heard(text), v
    return asyncio.run(run())


class _Gate:
    consults_running = 0
    calls_out = held = turn_in_flight = False


def test_the_callers_own_goodbye_sends_the_room_to_sleep():
    """#221's option 1: a turn that is only a goodbye is the sleep phrase -- the line, and
    the bridge's sleep once it has played."""
    for lang in RELEASE:
        missed = [s for s in CALLER[lang] if not _takes(lang, s)[0]]
        assert not missed, (lang, missed)
        taken = [s for s in NOT_CALLER[lang] if _takes(lang, s)[0]]
        assert not taken, (lang, taken)

    async def run():
        v = _Voice("it")
        await v._heard("che ore sono")
        assert await v._heard("Va bene, buonanotte!")
        assert v.said() == [i18n.get("it")._(goodbye.SLEEP_TEXT)]
        await v.speak()
        await until(lambda: v.sent(), timeout=2.0)
        assert v.sleep.sent and list(v.sleep.decisions) == [SENT]
    asyncio.run(run())


def test_a_greeting_as_the_first_words_is_no_goodbye():
    """Woken with "Buenas noches" (good evening) or "Ciao": hello, not good night -- the
    words go to the model. A plain goodbye is one from the first words on."""
    for lang, word in (("es", "Buenas noches."), ("it", "Ciao!")):
        assert not _takes(lang, word, first=True)[0], word
        assert _takes(lang, word)[0], word
    for lang, word in (("es", "Hasta mañana."), ("it", "Buonanotte."), ("en", "Good night.")):
        assert _takes(lang, word, first=True)[0], word


def test_the_callers_goodbye_stands_down_while_an_answer_is_owed():
    """The backstop's stand-downs (owed_reason): an answer still owed keeps the room
    awake and the words go to the model; a failure owed does not (they signed off). The
    request itself ("go to sleep") wins over anything owed, as before."""
    for lang, bye, request in (("en", "Good night.", "Go to sleep."),
                               ("es", "Hasta mañana.", "Vete a dormir."),
                               ("it", "Buonanotte.", "Vai a dormire.")):
        took, v = _takes(lang, bye, book=_Owed(["the weather"]))
        assert not took and v.said() == [], (lang, bye)
        assert list(v.sleep.decisions) == ["an answer is still owed"], v.sleep.decisions
        assert _takes(lang, bye, book=_Owed(["error"]))[0], (lang, "a failure owed")
        assert _takes(lang, request, book=_Owed(["the weather"]))[0], (lang, request)


def test_a_greeting_back_is_no_goodbye_for_the_backstop():
    """Review round 1, the BLOCKER: woken with "Ciao" (hello, which the sleep phrase
    leaves to the model), the model's "Ciao! Sono qui." is a hello back. Words that
    greet as well as part count in the reply unless the caller's turn was only such
    words, as their first in the session."""
    from test_goodbye import _sent, _turn
    for lang, caller, reply in (("it", "Ciao", "Ciao! Sono qui."),
                                ("it", "Ciao.", "Ciao, sono qui se hai bisogno."),
                                ("es", "Buenas noches.", "¡Buenas noches! Aquí estoy para lo que necesites."),
                                ("es", "Buenas noches", "¡Muy buenas noches! Estoy aquí si me necesitas.")):
        assert goodbye.greets_only(caller, i18n.get(lang)), caller
        sent, sleep = _sent(reply, lang=lang, messages=_turn(caller), first=True)
        assert sent == [] and sleep.decisions[-1] == "not a goodbye", (caller, reply, sleep.decisions)
    # A caller who did say goodbye: the same words back are one.
    for lang, caller, reply in (("it", "Va bene, a domani.", "Ciao!"),
                                ("es", "Bueno, hasta mañana.", "¡Buenas noches!")):
        assert not goodbye.greets_only(caller, i18n.get(lang)), caller
        sent, sleep = _sent(reply, lang=lang, messages=_turn(caller), first=True)
        assert len(sent) == 1 and sleep.decisions[-1] == SENT, (caller, reply, sleep.decisions)


def test_a_ciao_later_in_the_session_answered_with_ciao_sleeps():
    """Review round 2: the box ends "…alle otto. Ciao!", the caller answers "Ciao" at
    once (the sleep phrase stands down: maybe its echo), and the model says a bare
    "Ciao!": not their first words, so a goodbye -- the backstop sleeps."""
    from test_goodbye import _sent, _turn
    from teaport_brain.echo_guard import BotWords

    async def phrase():
        v = _Voice("it")
        v.bot_words = BotWords()
        await v._heard("che ore sono")
        v.bot_words.played("Certo, è alle otto. Ciao!")
        v._bot_speaking = True
        return await v._heard("Ciao"), v
    took, v = asyncio.run(phrase())
    assert not took and not v.first_words("Ciao") and v.first_words("che ore sono")
    for lang, caller, reply in (("it", "Ciao", "Ciao!"), ("it", "Ciao", "Ciao! A presto!"),
                                ("es", "Chao", "¡Chao!"), ("es", "Buenas noches", "¡Buenas noches!")):
        sent, sleep = _sent(reply, lang=lang, messages=_turn(caller), first=False)
        assert len(sent) == 1 and sleep.decisions[-1] == SENT, (caller, reply, sleep.decisions)


def test_the_boxs_last_word_heard_back_alone_does_not_sleep():
    """Review round 1: "Certo, è alle otto. Ciao!" heard back as "Ciao" while the box
    speaks, or just after, is likely its own voice: no sleep -- nor dropped, since one
    short word is the user's as often (the words go to the model as usual). Once it is
    quiet, the same word is the caller's goodbye."""
    from teaport_brain.echo_guard import BotWords

    async def run():
        v = _Voice("it")
        v.bot_words = BotWords()
        await v._heard("che ore sono")
        v.bot_words.played("Certo, è alle otto. Ciao!")
        v._bot_speaking = True
        assert not await v._heard("Ciao")
        assert v.said() == [] and list(v.sleep.decisions) == [
            "it may be the box's own last word heard back"]
        v._bot_speaking, v._quiet_since = False, v._clock() - wifi_voice.ECHO_TAIL_SECS - 1
        assert await v._heard("Ciao")
        assert v.said() == [i18n.get("it")._(goodbye.SLEEP_TEXT)]
        # Its own line's last word the same way ("Va bene, vado a dormire." -> "dormire"
        # is no goodbye; a line ending "a domani" is).
        v = _Voice("it")
        await v._heard("che ore sono")
        v.note_said("Ti richiamo, a domani")
        v._bot_speaking = True
        assert v.tail_echo("domani") and not v.tail_echo("buonanotte")
    asyncio.run(run())


def test_the_first_words_are_the_sessions_whatever_took_them():
    """Review round 1: a first turn another phrase took ("open my settings") still makes
    the next "buenas noches" a goodbye, not a hello."""
    class Settings:
        async def heard(self, voice, text):
            return text == "abre los ajustes"

    async def run():
        v = _Voice("es")
        v.phrases.insert(0, Settings())
        assert await v._heard("abre los ajustes")
        assert v.caller_spoke
        assert await v._heard("Buenas noches.")
        assert v.said() == [i18n.get("es")._(goodbye.SLEEP_TEXT)]
    asyncio.run(run())


def test_a_goodbye_while_a_reply_is_on_its_way_goes_to_the_model():
    """Review round 1: a completion still streaming (no consult out yet) would play its
    reply after the line: the goodbye goes to the model, whose own goodbye the backstop
    sleeps on. The request still wins."""
    gate = _Gate()
    gate.turn_in_flight = True
    for lang, bye, request in (("en", "Good night.", "Go to sleep."),
                               ("it", "Buonanotte.", "Vai a dormire.")):
        async def run(text):
            v = _Voice(lang)
            v.sleep.gate, v.sleep.owed = gate, _Owed()
            await v._heard("one thing")
            return await v._heard(text), v
        took, v = asyncio.run(run(bye))
        assert not took and list(v.sleep.decisions) == ["a reply is still on its way"], lang
        assert asyncio.run(run(request))[0], (lang, request)


def test_every_catalog_has_the_goodbye_patterns_and_only_es_and_it_their_own():
    for lang in i18n.catalogs():
        t = i18n.get(lang)
        for meaning, english in goodbye.PATTERNS.items():
            own = t.p("pattern", english)
            re.compile(own)
            if lang in ("es", "it"):
                assert own != english, (lang, meaning)
            else:
                assert own == english, (lang, meaning, "a language of its own: test it here")


def test_stop_listening_is_the_request_in_each_release_language():
    for lang, said in (("en", "Okay, stop listening."), ("es", "Deja de escuchar, gracias."),
                       ("it", "Smetti di ascoltare, grazie.")):
        assert wifi_voice.request_heard("sleep", said, i18n.get(lang)), (lang, said)


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok {name}")


if __name__ == "__main__":
    main()
    print("ALL PASS")
