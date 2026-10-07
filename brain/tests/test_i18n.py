"""Translations (i18n.py, locale/*/LC_MESSAGES/teaport.po): every string the code asks to
translate is in every language's catalog and nothing stale is, placeholders survive
translation, the "pattern" entries are valid regexes that hear their language, the
compiled .mo files are current (and read back as GNU msgfmt's would), and the setup
page and voice pick the right language."""
import ast
import gettext
import io
import os
import re
import urllib.request

import pytest

from teaport_brain import i18n, wifi_setup, wifi_voice

PKG = os.path.dirname(os.path.abspath(i18n.__file__))


def _modules() -> list[tuple[str, ast.Module]]:
    """Every module of the package that imports i18n: where translatable text can be."""
    out = []
    for name in sorted(os.listdir(PKG)):
        if not name.endswith(".py") or name == "i18n.py":
            continue
        tree = ast.parse(open(os.path.join(PKG, name), encoding="utf-8").read())
        if any((isinstance(n, ast.ImportFrom) and (n.module == "teaport_brain.i18n" or (
                n.module == "teaport_brain" and any(a.name == "i18n" for a in n.names))))
               or (isinstance(n, ast.Import) and any(a.name == "teaport_brain.i18n" for a in n.names))
               for n in ast.walk(tree)):
            out.append((name, tree))
    return out


# The calls that translate a msgid the code computes, by module and source, and where
# their msgids come from. _used() adds the tables; the reasons are N_() literals in the
# modules that set them. Any other non-literal argument fails the test: an f-string, a
# concatenation or a variable is text that would ship untranslated with nothing red.
DYNAMIC = {
    "wifi_setup.py": {
        "t._(reason)",                       # Setup.failure's reason: N_() in wifi.py / here
        "t._(error)",                        # parse_form's messages: N_() here
    },
    "wifi_voice.py": {
        "t.p('pattern', english)",           # PATTERNS
        "t.p('digit', c)", "t.p('digit', d)",  # 0-9
        "t.p('letter', c.upper())",          # A-Z, optional per catalog (LETTERS)
        "t.p('spell', _SYMBOLS[c])",         # _SYMBOLS
        "t._(self._why)",                    # status.json's reason: N_() in wifi.py / wifi_setup.py
        "t._(status.get('reason') or N_('something went wrong'))",  # the same
    },
}


def _calls():
    """(module, call node, its name) for every _(), p() and N_() call in _modules()."""
    for name, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if fname in ("_", "N_", "p"):
                    yield name, node, fname


def _literal(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def test_every_translated_string_is_one_the_tests_can_see():
    unknown = sorted(f"{name}: {ast.unparse(node)}" for name, node, _ in _calls()
                     if not all(_literal(a) for a in node.args) or node.keywords
                     if ast.unparse(node) not in DYNAMIC.get(name, set()))
    assert not unknown, ("translation calls with a computed argument -- make it a literal, "
                         f"or add it to DYNAMIC with where its msgids come from: {unknown}")
    stale = {f"{name}: {src}" for name, srcs in DYNAMIC.items() for src in srcs} - {
        f"{name}: {ast.unparse(node)}" for name, node, _ in _calls()}
    assert not stale, f"DYNAMIC lists calls that are gone: {sorted(stale)}"


def _used() -> set[str]:
    """(context\\x04)msgid for every t._("..."), t.p("ctx", "..."), N_("...") with literal
    arguments (ast: adjacent literals are already one string), plus the ones the code
    builds from tables: digits, symbol names, and the English patterns."""
    keys = set()
    for _name, node, fname in _calls():
        if not all(_literal(a) for a in node.args):
            continue  # DYNAMIC: the tables below, or N_() literals found here
        args = [a.value for a in node.args]
        if fname in ("_", "N_") and len(args) == 1:
            keys.add(args[0])
        elif fname == "p" and len(args) == 2:
            keys.add(args[0] + "\x04" + args[1])
    keys |= {"digit\x04" + str(d) for d in range(10)}
    keys |= {"spell\x04" + s for s in wifi_voice._SYMBOLS.values()}
    keys |= {"pattern\x04" + p for p in wifi_voice.PATTERNS.values()}
    return keys


def _catalog(lang) -> dict[str, str]:
    with open(i18n._po_path(lang), encoding="utf-8") as f:
        return {k: v for k, v in i18n.parse_po(f.read()).items() if k}


LANGS = [l for l in i18n.catalogs() if l != "en"]


# The languages the speech recognizer (Voxtral Mini 4B Realtime) understands.
VOXTRAL = {"en", "fr", "es", "de", "ru", "zh", "ja", "it", "pt_BR", "nl", "ar", "hi", "ko"}


def test_the_catalogs_cover_what_the_box_hears_and_says():
    assert set(i18n.catalogs()) == VOXTRAL
    assert set(i18n.ESPEAK_TO_LANG.values()) <= VOXTRAL       # the voice speaks a subset


# Letter names are optional per language (wifi_voice.spell): only a language whose
# letter names collide with its digits needs them, so a catalog may carry any of A-Z.
LETTERS = {"letter\x04" + chr(c) for c in range(ord("A"), ord("Z") + 1)}


@pytest.mark.parametrize("lang", LANGS)
def test_every_string_is_translated_and_none_is_stale(lang):
    used, have = _used(), set(_catalog(lang))
    assert not used - have, f"{lang}: untranslated {sorted(used - have)}"
    assert not have - used - LETTERS, f"{lang}: stale (no longer in the code) {sorted(have - used - LETTERS)}"


def test_korean_names_the_letters_its_digits_would_swallow():
    ko = i18n.get("ko")
    assert wifi_voice.spell("teaport-9e35", ko).split(ko.p("spell", ", "))[3] != ko.p("digit", "2")


# Which scripts a catalog's letters may come from (unicodedata names' first word, plus
# Latin for brand names, Wi-Fi, placeholders' surroundings and the like).
SCRIPTS = {"es": (), "pt_BR": (), "fr": (), "it": (), "de": (), "nl": (),
           "ru": ("CYRILLIC",), "ar": ("ARABIC",), "hi": ("DEVANAGARI",),
           "zh": ("CJK", "IDEOGRAPHIC", "FULLWIDTH"),
           "ja": ("CJK", "HIRAGANA", "KATAKANA", "IDEOGRAPHIC", "FULLWIDTH"),
           "ko": ("HANGUL", "CJK", "FULLWIDTH")}


@pytest.mark.parametrize("lang", sorted(set(LANGS) | {"en"}))
def test_no_broken_characters(lang):
    """What renders as tofu, garbage or nothing on a phone: not NFC, the replacement
    character, control / private-use / unassigned code points, mojibake (UTF-8 read as
    Latin-1), or letters from a script the language does not use."""
    import unicodedata
    allowed = ("LATIN",) + SCRIPTS.get(lang, ())
    for key, text in _catalog(lang).items():
        where = f"{lang} {key.split(chr(4))[-1][:40]!r}"
        assert unicodedata.normalize("NFC", text) == text, f"{where}: not NFC"
        assert "\ufffd" not in text, f"{where}: replacement character"
        assert not re.search(r"Ã.|Â.|â€|ï»¿", text), f"{where}: mojibake"
        for c in text:
            cat = unicodedata.category(c)
            assert cat not in ("Cc", "Co", "Cs", "Cn"), f"{where}: {cat} U+{ord(c):04X}"
            if cat[0] in "LM" and not key.startswith("pattern\x04"):
                assert unicodedata.name(c, "?").startswith(allowed), (
                    f"{where}: {c!r} ({unicodedata.name(c, '?')}) is not {lang}'s script")


def test_english_needs_only_its_digit_words():
    assert set(_catalog("en")) == {"digit\x04" + str(d) for d in range(10)}


@pytest.mark.parametrize("lang", LANGS)
def test_placeholders_survive_translation(lang):
    for key, text in _catalog(lang).items():
        msgid = key.split("\x04")[-1]
        if key.startswith("pattern\x04"):
            continue  # regex quantifiers like {0,3} are not placeholders
        assert set(re.findall(r"{(\w+)}", msgid)) == set(re.findall(r"{(\w+)}", text)), (lang, msgid)


SAYS = {  # what a speaker of each language says for each meaning (from the translators)
    "es": {"start": "conéctate a otra red wifi", "yes": "sí, claro", "no": "ahora no",
           "cancel": "detente", "repeat": "¿cuál era la contraseña?"},
    "pt_BR": {"start": "quero configurar o wi-fi", "yes": "pode ser", "no": "deixa pra lá",
              "cancel": "chega", "repeat": "repete"},
    "fr": {"start": "Connecte-toi au Wi-Fi", "yes": "ouais vas-y", "no": "non merci, pas maintenant",
           "cancel": "arrête la configuration", "repeat": "tu peux répéter ?"},
    "it": {"start": "Collegati al wi-fi", "yes": "sì grazie", "no": "no grazie, non ora",
           "cancel": "fermati", "repeat": "puoi ripetere?"},
    "de": {"start": "Richte das WLAN ein", "yes": "Ja, mach das", "no": "Nein danke",
           "cancel": "Brich ab", "repeat": "Wiederhole das bitte"},
    "nl": {"start": "Stel de wifi in", "yes": "Ja graag", "no": "Liever niet",
           "cancel": "Annuleer", "repeat": "Nog een keer"},
    "ru": {"start": "Давай настроим вайфай", "yes": "да, давай", "no": "нет, не надо",
           "cancel": "отмени настройку", "repeat": "повтори пароль"},
    "ar": {"start": "أريد إعداد الواي فاي", "yes": "نعم من فضلك", "no": "لا شكرا",
           "cancel": "أوقف الإعداد", "repeat": "كرر من فضلك"},
    "hi": {"start": "मुझे वाई-फ़ाई सेट अप करना है", "yes": "हाँ, शुरू करो", "no": "नहीं, रहने दो",
           "cancel": "कैंसल कर दो", "repeat": "पासवर्ड दोबारा बोलिए"},
    "zh": {"start": "帮我设置一下无线网络", "yes": "好的，开始吧", "no": "不用了",
           "cancel": "取消设置", "repeat": "再说一遍密码"},
    "ja": {"start": "Wi-Fiを設定して", "yes": "はい、お願いします", "no": "いいえ、やめておきます",
           "cancel": "設定を中止して", "repeat": "もう一回言って"},
    "ko": {"start": "와이파이 설정해 줘", "yes": "네 해 주세요", "no": "아니요 됐어요",
           "cancel": "취소해 주세요", "repeat": "다시 말해 줘"},
}


@pytest.mark.parametrize("lang", LANGS)
def test_the_patterns_hear_their_language(lang):
    t = i18n.get(lang)
    for meaning, english in wifi_voice.PATTERNS.items():
        re.compile(t.p("pattern", english))                      # a valid regex
        assert wifi_voice.heard(meaning, SAYS[lang][meaning], t), (lang, meaning)
    assert wifi_voice.is_yes(SAYS[lang]["yes"], t)               # said as an answer
    assert wifi_voice.heard("start", "set up wifi", t)           # English works too...
    assert not wifi_voice.heard("start", SAYS[lang]["yes"], t)
    assert not wifi_voice.heard("yes", SAYS[lang]["no"], t) or wifi_voice.heard("no", SAYS[lang]["no"], t)


START = {  # more ways to ask for setup, inflected forms first (#96), and talk that isn't
    "es": (["configurando el wifi", "conectándome a la red wifi", "cambiando de wifi"],
           ["mi wifi va lento", "¿qué wifi es este?", "no estoy cambiando de wifi"]),
    "pt_BR": (["configurando o wi-fi", "conectando no wi-fi", "trocando de wi-fi", "mudando o wifi"],
              ["meu wi-fi está lento", "a rede caiu", "não tô conectando no wi-fi"]),
    "fr": (["en configurant le wifi", "connectant au wi-fi"], ["mon wifi est lent"]),
    "it": (["configurando il wi-fi", "collegando al wifi"], ["il mio wifi è lento"]),
    "de": (["Verbind dich mit dem WLAN", "WLAN einrichten"], ["Mein WLAN ist langsam"]),
    "nl": (["verbind met de wifi", "wifi instellen"], ["mijn wifi is traag"]),
    "ru": (["давай настраивать вайфай", "подключайся к вайфаю", "переключаться на другой вайфай",
            "меняй вайфай"],
           ["у меня вайфай плохой", "вайфай тормозит", "телефон не подключается к вайфаю",
            "не меняй вайфай", "не надо настраивать вайфай", "не подключайся к чужому вайфаю",
            "нельзя переключаться на другой вайфай", "мы подключаемся к вайфаю в кафе"]),
    "ar": (["اتصل بالواي فاي"], ["الواي فاي بطيء"]),
    "hi": (["वाई-फ़ाई कनेक्ट करो"], ["मेरा वाई-फ़ाई धीमा है"]),
    "zh": (["连接无线网", "切换wifi"], ["我的wifi很慢"]),
    "ja": (["Wi-Fiに接続して", "Wi-Fiを切り替えて"], ["Wi-Fiが遅い"]),
    "ko": (["와이파이 연결해 줘", "와이파이 바꿔 줘"], ["와이파이가 느려"]),
}


@pytest.mark.parametrize("lang", LANGS)
def test_the_start_pattern_hears_requests_not_talk(lang):
    t = i18n.get(lang)
    heard, talk = START[lang]
    assert [s for s in heard if not wifi_voice.heard("start", s, t)] == []
    assert [s for s in talk if wifi_voice.heard("start", s, t)] == []


def test_arabic_is_heard_with_or_without_its_diacritics():
    ar = i18n.get("ar")
    for vocalised in ("نَعَم", "نعم"):
        assert wifi_voice.heard("yes", vocalised, ar), vocalised


def test_network_names_keep_their_direction_in_arabic():
    page = wifi_setup.render_page([{"ssid": "Guest 5G!", "signal": 70, "open": True}],
                                  "", i18n.get("ar"))
    assert '<bdi>Guest 5G!</bdi>' in page and '<small dir="ltr">' in page
    assert "<bdi>HomeNet</bdi>" in wifi_setup.render_joining("HomeNet", i18n.get("ar"))


def test_a_foreign_no_is_not_englishs_no():
    # ...except "no", which is an ordinary word elsewhere: Portuguese "no celular" (in
    # the phone), Italian "come no" (of course). Only the language's own "no" counts.
    assert not wifi_voice.heard("no", "sim, faço no celular", i18n.get("pt_BR"))
    assert wifi_voice.heard("no", "não", i18n.get("pt_BR"))
    assert wifi_voice.heard("no", "no thanks", i18n.get("en"))


def test_the_compiled_catalogs_are_current_and_read_like_gnu_msgfmt():
    assert i18n.main(["--check"]) == 0, "run: python -m teaport_brain.i18n"
    for lang in i18n.catalogs():
        mo = gettext.GNUTranslations(io.BytesIO(i18n.compiled(lang)))
        po = _catalog(lang)
        assert {k: v for k, v in mo._catalog.items() if k} == po, lang


def test_the_session_language_picks_the_catalog():
    st = {"ssid": "teaport-9e35", "password": "47190352"}
    for code, lang in i18n.ESPEAK_TO_LANG.items():
        t = i18n.for_espeak(code)
        assert t.lang == lang
        text = wifi_voice.instructions(st, t)
        sep = t.p("spell", ", ")
        # The password digit by digit, in this language's digit words and separator.
        assert sep.join(t.p("digit", d) for d in "47190352") in text, (code, text)
        assert t.p("spell", "dash") in text
        for extra in ({"screen": True}, {"screen": True, "qr": True}):
            screen = wifi_voice.instructions(dict(st, **extra), t)[len(text):]
            assert screen.strip() and (lang == "en" or "screen" not in screen), (code, screen)
    assert i18n.for_espeak("xx").lang == "en"
    # TTS_LANGUAGE / ?language= give plain and regional codes too, in any case.
    for code, lang in (("fr", "fr"), ("es-MX", "es"), ("ja_JP", "ja"), ("pt", "pt_BR"),
                       ("PT-BR", "pt_BR"), ("EN-US", "en"), ("zh-CN", "zh"), ("de", "de")):
        assert i18n.for_espeak(code).lang == lang, code
    fr = i18n.get("fr")
    assert wifi_voice.spell("Tito2017", fr).startswith(fr.p("spell", "capital {letter}").format(letter="T"))


@pytest.mark.parametrize("header,lang", [
    ("es-MX,es;q=0.9,en;q=0.8", "es"), ("pt-PT,pt;q=0.9", "pt_BR"), ("ja-JP", "ja"),
    ("zh-CN,zh;q=0.9", "zh"), ("de-DE,de;q=0.9", "de"), ("ar-EG", "ar"), ("ko-KR,ko;q=0.9", "ko"),
    ("sv-SE,sv;q=0.9", "en"), ("fr;q=0,it;q=0.5", "it"), (None, "en"),
    ("es;Q=0, fr", "fr"), ("FR-ca", "fr"), ("*", "en"), ("de;level=1;q=0.3,it;q=0.4", "it"),
    # Every Chinese gets the Simplified catalog, the only one (i18n._ALIASES says why).
    ("zh-TW", "zh"), ("zh-Hant-HK,en;q=0.5", "zh")])
def test_the_page_follows_the_phones_language(header, lang):
    assert i18n.for_accept_language(header).lang == lang


def test_the_page_is_served_in_the_phones_language():
    setup = wifi_setup.Setup(nm=None, status=None, port=0, bind="127.0.0.1")
    setup.networks = [{"ssid": "home", "signal": 80, "open": True, "security": "open"}]
    setup.failure = ("home", "the password did not work")
    setup.serve()
    try:
        def get(lang):
            req = urllib.request.Request(f"http://127.0.0.1:{setup.port}/",
                                         headers={"Accept-Language": lang})
            return urllib.request.urlopen(req).read().decode()
        pages = {lang: get(header) for lang, header in
                 (("es", "es-ES,es;q=0.9"), ("en", "en-US"), ("ar", "ar"))}
    finally:
        setup.server.shutdown()
    import html as html_mod
    for lang, page in pages.items():
        t = i18n.get(lang)
        assert f'<html lang="{lang}" dir="{t.dir}">' in page
        assert html_mod.escape(t._("Connect to Wi-Fi")) in page
        why = t._("Could not join {network}: {reason}. Try again.").format(
            network="home", reason=t._("the password did not work"))
        assert html_mod.escape(why) in re.sub(r"</?bdi>", "", page), lang
    assert 'dir="rtl"' in pages["ar"] and 'dir="ltr"' in pages["es"]


def test_the_language_picker_overrides_the_phone_and_is_remembered():
    setup = wifi_setup.Setup(nm=None, status=None, port=0, bind="127.0.0.1")
    setup.networks = [{"ssid": "home", "signal": 80, "open": False, "security": "WPA2"}]
    setup.serve()
    base = f"http://127.0.0.1:{setup.port}"

    def get(path, headers):
        with urllib.request.urlopen(urllib.request.Request(base + path, headers=headers)) as r:
            return r.read().decode(), r.headers.get("Set-Cookie")
    try:
        # A Spanish phone picks Japanese: the page switches and the choice is kept.
        page, cookie = get("/?lang=ja", {"Accept-Language": "es"})
        assert '<html lang="ja"' in page and cookie.startswith("teaport_lang=ja")
        assert '<option value="ja" selected>日本語</option>' in page
        assert 'action="/connect?lang=ja"' in page          # the Joining page follows it
        page, _ = get("/", {"Accept-Language": "es", "Cookie": "teaport_lang=ja"})
        assert '<html lang="ja"' in page
        # An unknown choice is ignored: back to the phone's language.
        page, cookie = get("/?lang=xx", {"Accept-Language": "es"})
        assert '<html lang="es"' in page and cookie is None
        # Every catalog is offered, by its own name.
        for code in i18n.catalogs():
            assert f'<option value="{code}"' in page and i18n.ENDONYMS[code] in page
    finally:
        setup.server.shutdown()


def test_a_choice_names_a_catalog_however_it_is_written():
    assert i18n.chosen("pt-BR").lang == i18n.chosen("pt_br").lang == "pt_BR"
    assert i18n.chosen("EN").lang == "en"
    for bad in (None, "", "xx", "../en", "pt"):
        assert i18n.chosen(bad) is None, bad


HEADER = 'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'


def test_fuzzy_entries_are_left_out_as_msgfmt_leaves_them():
    po = HEADER + '#, fuzzy\nmsgid "a"\nmsgstr "A"\n\n#, c-format, fuzzy\nmsgid "b"\nmsgstr "B"\n'
    po += '#: x.py:1\nmsgid "c"\nmsgstr "C"\n#, fuzzy\nmsgid "d"\nmsgstr "D"\nmsgid "e"\nmsgstr "E"'
    assert set(i18n.parse_po(po)) == {"", "c", "e"}
    # The header is kept fuzzy or not, as msgfmt keeps it.
    assert "" in i18n.parse_po("#, fuzzy\n" + HEADER)


def test_an_entry_without_a_msgstr_is_an_error_not_a_context_for_the_next():
    with pytest.raises(ValueError, match="line 4"):
        i18n.parse_po('msgctxt "c"\nmsgid "a"\n\nmsgid "b"\nmsgstr "B"\n')
    po = 'msgctxt "c"\nmsgid "a"\nmsgstr "A"\nmsgid "b"\nmsgstr "B"\n'  # no blank line
    assert i18n.parse_po(po) == {"c\x04a": "A", "b": "B"}


if __name__ == "__main__":
    # test_suite.py runs every test file as a script: without this it would pass here
    # having run nothing.
    import sys
    sys.exit(pytest.main([__file__, "-q", "-p", "no:cacheprovider"]))
