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

SOURCES = ["wifi_voice.py", "wifi_setup.py", "wifi.py"]
PKG = os.path.dirname(os.path.abspath(i18n.__file__))


def _used() -> set[str]:
    """(context\\x04)msgid for every t._("..."), t.p("ctx", "..."), N_("...") with literal
    arguments (ast: adjacent literals are already one string), plus the ones the code
    builds from tables: digits, symbol names, and the English patterns."""
    keys = set()
    for name in SOURCES:
        tree = ast.parse(open(os.path.join(PKG, name), encoding="utf-8").read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if fname in ("_", "N_") and len(args) == 1 and len(node.args) == 1:
                keys.add(args[0])
            elif fname == "p" and len(args) == 2 and len(node.args) == 2:
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


@pytest.mark.parametrize("lang", LANGS)
def test_every_string_is_translated_and_none_is_stale(lang):
    used, have = _used(), set(_catalog(lang))
    assert not used - have, f"{lang}: untranslated {sorted(used - have)}"
    assert not have - used, f"{lang}: stale (no longer in the code) {sorted(have - used)}"


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
    assert wifi_voice.heard("start", "set up wifi", t)           # English works too...
    assert not wifi_voice.heard("start", SAYS[lang]["yes"], t)
    assert not wifi_voice.heard("yes", SAYS[lang]["no"], t) or wifi_voice.heard("no", SAYS[lang]["no"], t)


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
    assert i18n.for_espeak("xx").lang == "en"
    fr = i18n.get("fr")
    assert wifi_voice.spell("Tito2017", fr).startswith(fr.p("spell", "capital {letter}").format(letter="T"))


@pytest.mark.parametrize("header,lang", [
    ("es-MX,es;q=0.9,en;q=0.8", "es"), ("pt-PT,pt;q=0.9", "pt_BR"), ("ja-JP", "ja"),
    ("zh-CN,zh;q=0.9", "zh"), ("de-DE,de;q=0.9", "de"), ("ar-EG", "ar"), ("ko-KR,ko;q=0.9", "ko"),
    ("sv-SE,sv;q=0.9", "en"), ("fr;q=0,it;q=0.5", "it"), (None, "en")])
def test_the_page_follows_the_phones_language(header, lang):
    assert i18n.for_accept_language(header).lang == lang


def test_the_page_is_served_in_the_phones_language():
    setup = wifi_setup.Setup(nm=None, status=None, port=0)
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
        assert html_mod.escape(why) in page, lang
    assert 'dir="rtl"' in pages["ar"] and 'dir="ltr"' in pages["es"]
