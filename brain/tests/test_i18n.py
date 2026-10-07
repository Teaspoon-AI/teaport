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


def test_the_catalogs_cover_the_engine_languages():
    assert set(i18n.ESPEAK_TO_LANG.values()) == set(i18n.catalogs())


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


SAYS = {  # what a speaker of each language says for each meaning
    "es": {"start": "oye, configura el wifi", "yes": "sí, claro", "no": "no, déjalo",
           "cancel": "cancela eso", "repeat": "¿me repites la contraseña?"},
    "fr": {"start": "tu peux configurer le wifi ?", "yes": "oui, vas-y", "no": "non merci",
           "cancel": "annule", "repeat": "répète le mot de passe"},
    "it": {"start": "puoi configurare il wifi?", "yes": "sì, certo", "no": "no, lascia stare",
           "cancel": "annulla", "repeat": "ripeti la password"},
    "pt_BR": {"start": "configura o wi-fi pra mim", "yes": "sim, pode", "no": "não, deixa pra lá",
              "cancel": "cancela", "repeat": "repete a senha"},
    "hi": {"start": "वाई-फ़ाई सेट अप करो", "yes": "हाँ, ठीक है", "no": "नहीं",
           "cancel": "रद्द करो", "repeat": "पासवर्ड दोबारा बताओ"},
    "ja": {"start": "Wi-Fiを設定して", "yes": "はい、お願いします", "no": "いいえ",
           "cancel": "キャンセルして", "repeat": "もう一度言って"},
    "zh": {"start": "帮我设置一下无线网", "yes": "好的", "no": "不用了",
           "cancel": "取消吧", "repeat": "再说一遍"},
}


@pytest.mark.parametrize("lang", LANGS)
def test_the_patterns_hear_their_language(lang):
    t = i18n.get(lang)
    for meaning, english in wifi_voice.PATTERNS.items():
        re.compile(t.p("pattern", english))                      # a valid regex
        assert wifi_voice.heard(meaning, SAYS[lang][meaning], t), (lang, meaning)
    assert wifi_voice.heard("start", "set up wifi", t)           # English always works
    assert not wifi_voice.heard("start", SAYS[lang]["yes"], t)


def test_the_compiled_catalogs_are_current_and_read_like_gnu_msgfmt():
    assert i18n.main(["--check"]) == 0, "run: python -m teaport_brain.i18n"
    for lang in i18n.catalogs():
        mo = gettext.GNUTranslations(io.BytesIO(i18n.compiled(lang)))
        po = _catalog(lang)
        assert {k: v for k, v in mo._catalog.items() if k} == po, lang


def test_the_session_language_picks_the_catalog():
    st = {"ssid": "teaport-9e35", "password": "47190352"}
    es = wifi_voice.instructions(st, i18n.for_espeak("es"))
    assert es.startswith("En tu teléfono") and "guion, nueve, E, tres, cinco" in es
    assert "cuatro, siete, uno, nueve" in es and "teaport guion nueve E tres cinco punto local" in es
    ja = wifi_voice.instructions(st, i18n.for_espeak("ja"))
    assert "ハイフン、きゅう、E、さん、ご" in ja
    assert i18n.for_espeak("en-gb").lang == "en" and i18n.for_espeak("xx").lang == "en"
    assert wifi_voice.spell("Tito2017", i18n.for_espeak("fr-fr")).startswith("T majuscule, I, T, O, deux")


@pytest.mark.parametrize("header,lang", [
    ("es-MX,es;q=0.9,en;q=0.8", "es"), ("pt-PT,pt;q=0.9", "pt_BR"), ("ja-JP", "ja"),
    ("zh-CN,zh;q=0.9", "zh"), ("de-DE,de;q=0.9", "en"), ("fr;q=0,it;q=0.5", "it"), (None, "en")])
def test_the_page_follows_the_phones_language(header, lang):
    assert i18n.for_accept_language(header).lang == lang


def test_the_page_is_served_in_the_phones_language():
    import threading
    setup = wifi_setup.Setup(nm=None, status=None, port=0)
    setup.networks = [{"ssid": "home", "signal": 80, "open": True, "security": "open"}]
    setup.failure = ("home", "the password did not work")
    setup.serve()
    try:
        def get(lang):
            req = urllib.request.Request(f"http://127.0.0.1:{setup.port}/",
                                         headers={"Accept-Language": lang})
            return urllib.request.urlopen(req).read().decode()
        es, en = get("es-ES,es;q=0.9"), get("en-US")
    finally:
        setup.server.shutdown()
    assert '<html lang="es">' in es and "Conectar al wifi" in es
    assert "No se pudo conectar a home: la contraseña no funcionó." in es and "abierta" in es
    assert '<html lang="en">' in en and "Could not join home: the password did not work." in en
