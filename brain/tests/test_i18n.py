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
    "ai_account.py": {
        "t._(self.reason)",                  # a key check's reason: N_() there (#279)
    },
    "config_ui.py": {
        "t._(v)",                            # ACCOUNT_TEXT's values: N_() there (#279)
    },
    "settings_access.py": {                  # its lines: N_() there (#279)
        "t._(SHOWN_TEXT)", "t._(SPOKEN_TEXT)", "t._(OFFLINE_TEXT)", "t._(UNAVAILABLE_TEXT)",
    },
    "llm_error_speaker.py": {
        "t._(text)",                         # its four notices: N_() there
    },
    "sip_server.py": {
        "t._(NO_MODEL_CALL_TEXT)",           # N_() in llm_error_speaker.py (#277)
    },
    "wifi_setup.py": {
        "t._(ACCOUNT_HELP)",                 # N_() there (#279)
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
        "self.t._(WELCOME_TEXT)", "self.t._(AGAIN_TEXT)",  # N_() there (#278)
        "self.t._(ONLINE_NOW_TEXT)",
        "t._(ACCOUNT_SAVED_TEXT if saved else ACCOUNT_NOT_SAVED_TEXT)",  # N_() there (#279)
    },
    "agent_session.py": {
        "t._(self._BUSY_MESSAGE)",           # the class constants: N_() wraps them
        "t._(self._UNAVAILABLE_MESSAGE)",    # the same, at both push sites
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
           "cancel": "detente", "repeat": "¿cuál era la contraseña?", "settings": "abre los ajustes"},
    "pt_BR": {"start": "quero configurar o wi-fi", "yes": "pode ser", "no": "deixa pra lá",
              "cancel": "chega", "repeat": "repete", "settings": "abrir as configurações"},
    "fr": {"start": "Connecte-toi au Wi-Fi", "yes": "ouais vas-y", "no": "non merci, pas maintenant",
           "cancel": "arrête la configuration", "repeat": "tu peux répéter ?", "settings": "ouvre les réglages"},
    "it": {"start": "Collegati al wi-fi", "yes": "sì grazie", "no": "no grazie, non ora",
           "cancel": "fermati", "repeat": "puoi ripetere?", "settings": "Apri le impostazioni"},
    "de": {"start": "Richte das WLAN ein", "yes": "Ja, mach das", "no": "Nein danke",
           "cancel": "Brich ab", "repeat": "Wiederhole das bitte", "settings": "Öffne die Einstellungen"},
    "nl": {"start": "Stel de wifi in", "yes": "Ja graag", "no": "Liever niet",
           "cancel": "Annuleer", "repeat": "Nog een keer", "settings": "Open de instellingen"},
    "ru": {"start": "Давай настроим вайфай", "yes": "да, давай", "no": "нет, не надо",
           "cancel": "отмени настройку", "repeat": "повтори пароль", "settings": "Открой настройки"},
    "ar": {"start": "أريد إعداد الواي فاي", "yes": "نعم من فضلك", "no": "لا شكرا",
           "cancel": "أوقف الإعداد", "repeat": "كرر من فضلك", "settings": "افتح الإعدادات"},
    "hi": {"start": "मुझे वाई-फ़ाई सेट अप करना है", "yes": "हाँ, शुरू करो", "no": "नहीं, रहने दो",
           "cancel": "कैंसल कर दो", "repeat": "पासवर्ड दोबारा बोलिए", "settings": "सेटिंग्स खोलो"},
    "zh": {"start": "帮我设置一下无线网络", "yes": "好的，开始吧", "no": "不用了",
           "cancel": "取消设置", "repeat": "再说一遍密码", "settings": "打开设置"},
    "ja": {"start": "Wi-Fiを設定して", "yes": "はい、お願いします", "no": "いいえ、やめておきます",
           "cancel": "設定を中止して", "repeat": "もう一回言って", "settings": "設定を開いて"},
    "ko": {"start": "와이파이 설정해 줘", "yes": "네 해 주세요", "no": "아니요 됐어요",
           "cancel": "취소해 주세요", "repeat": "다시 말해 줘", "settings": "설정 열어 줘"},
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


# "open my settings" (settings_access.py, #279): ways to ask, and talk about settings that
# is not a request -- the phone's, Wi-Fi, network, Bluetooth or app settings among it -- heard as SettingsPhrase hears it (no question, at most
# PHRASE_MAX_WORDS words).
SETTINGS = {
    "es": (["abre los ajustes", "Teaport, abre tus ajustes", "por favor, abre la configuración", "muéstrame mis ajustes"],
           ["abre los ajustes del teléfono", "abre los ajustes de wifi", "abre la configuración de red", "abre los ajustes de bluetooth", "en el móvil, abre los ajustes", "abre los ajustes de la app", "me gustan tus ajustes", "los ajustes están bien así"]),
    "pt_BR": (["abrir as configurações", "Teaport, abre as suas configurações", "por favor, abre as configurações", "me mostra minhas configurações"],
           ["abre as configurações do celular", "abre as configurações do wi-fi", "abre as configurações de rede", "abre as configurações do bluetooth", "no celular, abre as configurações", "abre as configurações do aplicativo", "gostei das configurações", "as configurações estão boas"]),
    "fr": (["ouvre les réglages", "Teaport, ouvre tes réglages", "svp, affiche les paramètres", "ouvre mes réglages maintenant"],
           ["ouvre les réglages du téléphone", "ouvre les réglages Wi-Fi", "ouvre les paramètres réseau", "ouvre les réglages Bluetooth", "sur mon téléphone, ouvre les réglages", "ouvre les réglages de l'appli", "j'aime bien tes réglages", "les réglages sont bons comme ça"]),
    "it": (["apri le impostazioni", "Teaport, apri le tue impostazioni", "per favore, mostrami le impostazioni", "apri le mie impostazioni"],
           ["apri le impostazioni del telefono", "apri le impostazioni wifi", "apri le impostazioni di rete", "apri le impostazioni bluetooth", "sul telefono, apri le impostazioni", "apri le impostazioni dell'app", "le impostazioni vanno bene", "ho cambiato le impostazioni"]),
    "de": (["Öffne die Einstellungen", "Teaport, öffne deine Einstellungen", "Bitte zeig mir die Einstellungen", "Öffne meine Einstellungen"],
           ["Öffne die Handy-Einstellungen", "Öffne die WLAN-Einstellungen", "Öffne die Netzwerkeinstellungen", "Öffne die Bluetooth Einstellungen", "Auf dem Handy öffne die Einstellungen", "Öffne die Einstellungen der App", "Die Einstellungen passen so", "Ich mag deine Einstellungen"]),
    "nl": (["Open de instellingen", "Teaport, open je instellingen", "Alsjeblieft, toon de instellingen", "Open mijn instellingen"],
           ["Open de telefooninstellingen", "Open de wifi-instellingen", "Open de netwerkinstellingen", "Open de bluetooth instellingen", "Op mijn telefoon, open de instellingen", "Open de instellingen van de app", "De instellingen zijn prima", "Ik vind je instellingen mooi"]),
    "ru": (["Открой настройки", "Teaport, открой свои настройки", "Пожалуйста, покажи мне настройки", "Открой мои настройки"],
           ["Открой настройки телефона", "Открой настройки Wi-Fi", "Открой сетевые настройки", "Открой настройки блютуза", "На телефоне открой настройки", "Открой настройки приложения", "Настройки и так хорошие", "Мне нравятся твои настройки"]),
    "ar": (["افتح الإعدادات", "يا Teaport، افتح إعداداتك", "من فضلك اعرض الإعدادات", "افتح لي الإعدادات الآن"],
           ["افتح إعدادات الهاتف", "افتح إعدادات الواي فاي", "افتح إعدادات الشبكة", "افتح إعدادات البلوتوث", "في الهاتف افتح الإعدادات", "افتح إعدادات التطبيق", "الإعدادات جيدة الآن", "أحب إعداداتك"]),
    "hi": (["सेटिंग्स खोलो", "मेरी सेटिंग्स खोलो", "Teaport की सेटिंग्स खोलो", "ज़रा अपनी सेटिंग्स दिखाओ"],
           ["फ़ोन की सेटिंग्स खोलो", "अपने फ़ोन की सेटिंग्स खोलो", "वाई-फ़ाई की सेटिंग्स खोलो", "नेटवर्क सेटिंग्स खोलो", "ब्लूटूथ सेटिंग्स खोलो", "ऐप की सेटिंग्स खोलो", "खोलो फ़ोन की सेटिंग्स", "सेटिंग्स ठीक हैं", "मुझे सेटिंग्स पसंद हैं"]),
    "zh": (["好的，打开设置", "打开设置", "Teaport，打开你的设置", "请帮我打开设置页面", "把设置打开"],
           ["打开手机设置", "打开 Wi-Fi 设置", "打开网络设置", "打开蓝牙设置", "手机，打开设置", "打开应用的设置", "设置这样就好", "我喜欢你的设置"]),
    "ja": (["設定を開いて", "ティーポート、設定を開いて", "あなたの設定を見せてください", "ちょっと設定画面を開いて"],
           ["スマホの設定を開いて", "Wi-Fi 設定を開いて", "ネットワーク 設定を開いて", "Bluetooth の設定を開いて", "スマホ、設定を開いて", "アプリの設定を開いて", "設定はこれでいい", "設定が好きです"]),
    "ko": (["설정 열어 줘", "티포트야, 설정 열어 줘", "내 설정 보여 줘", "그럼 설정 페이지 열어 주세요"],
           ["휴대폰 설정 열어 줘", "와이파이 설정 열어 줘", "네트워크 설정 열어 줘", "블루투스 설정 열어 줘", "내 휴대폰 설정 열어 줘", "앱 설정 열어 줘", "설정은 이대로 좋아", "설정이 마음에 들어"]),
}


def _settings_heard(text: str, t) -> bool:
    from teaport_brain import settings_access
    return (wifi_voice.heard("settings", text, t) and not wifi_voice._QUESTION.search(text)
            and wifi_voice.words(text) <= settings_access.PHRASE_MAX_WORDS)


@pytest.mark.parametrize("lang", LANGS)
def test_the_settings_pattern_hears_requests_not_talk(lang):
    t = i18n.get(lang)
    asks, talk = SETTINGS[lang]
    assert [s for s in asks if not _settings_heard(s, t)] == []
    assert [s for s in talk if _settings_heard(s, t)] == []
    assert _settings_heard("open my settings", t)                # English works too
    # Some other thing's settings are never the box's (#279 review).
    for other in ("open the phone settings", "open wifi settings", "open my phone's settings",
                  "open the network settings", "open bluetooth settings",
                  "on my phone, open the settings", "in the app, open settings",
                  "then open the settings"):
        assert not _settings_heard(other, t), other
    # Neither phrase is the other's.
    assert [s for s in asks if wifi_voice.heard("start", s, t)] == []


# The quotation marks the catalogs put round a phrase to say, opening -> closing.
_QUOTES = {'"': '"', "“": "”", "„": "“", "«": "»", "‘": "’", "'": "'", "「": "」"}


def _quoting_lines():
    from teaport_brain import settings_access
    from teaport_brain.llm_error_speaker import NO_MODEL_SETTINGS_TEXT, NO_MODEL_WIFI_TEXT
    return {"no model": ("start", NO_MODEL_WIFI_TEXT), "again": ("start", wifi_voice.AGAIN_TEXT),
            "offline": ("start", settings_access.OFFLINE_TEXT),
            "no account": ("settings", NO_MODEL_SETTINGS_TEXT),
            "key not saved": ("settings", wifi_voice.ACCOUNT_NOT_SAVED_TEXT),
            "new code": ("settings", 'Ask Teaport for a new one: say "open my settings".')}


@pytest.mark.parametrize("which", ["no model", "again", "offline", "no account", "key not saved",
                                   "new code"])
@pytest.mark.parametrize("lang", i18n.catalogs())
def test_the_phrase_a_line_quotes_starts_setup_in_its_language(lang, which):
    """The lines that tell the user what to say -- the no-model lines
    (llm_error_speaker.py, #277, #279), the first-boot setup's last line
    (wifi_voice.AGAIN_TEXT, #278), the settings phrase's own (settings_access.py,
    wifi_voice.ACCOUNT_NOT_SAVED_TEXT, #279): those exact words, said back, must be the
    phrase they name in that language (setup mode's start, or "open my settings")."""
    t = i18n.get(lang)
    meaning, msgid = _quoting_lines()[which]
    line = t._(msgid)
    # English: the apostrophes of "can't"/"I'm" before the quote are not quotes.
    sentence = line[line.index('"'):] if lang == "en" else line
    quoted = [m.group(2) for m in re.finditer(
        "(" + "|".join(map(re.escape, _QUOTES)) + r")\s*(.+?)\s*(?:"
        + "|".join(map(re.escape, set(_QUOTES.values()))) + ")", sentence)]
    assert len(quoted) == 1, (lang, line, quoted)
    if meaning == "settings":
        assert _settings_heard(quoted[0], t), (lang, quoted[0])
    else:
        assert wifi_voice.heard("start", quoted[0], t), (lang, quoted[0])


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


@pytest.mark.parametrize("lang", sorted(i18n.catalogs()))
def test_the_default_password_is_spoken_character_by_character(lang):
    """The default setup password (wifi_setup.setup_password: three groups of four) read
    out in every language: one spoken item per character, "dash" between the groups, and
    no two characters of its alphabet said alike -- a reader writes down what they hear."""
    t = i18n.get(lang)
    sep = t.p("spell", ", ")
    said = {c: wifi_voice.spell(c, t).strip() for c in wifi_setup.PASSWORD_ALPHABET + "-"}
    clash = {}
    for c, word in said.items():
        clash.setdefault(word.casefold(), []).append(c)
    assert not [v for v in clash.values() if len(v) > 1], (lang, clash)
    assert said["-"] == t.p("spell", "dash")
    pw = wifi_setup.setup_password("")
    items = wifi_voice.spell(pw, t).split(sep)
    assert len(items) == 14 and items[4] == items[9] == t.p("spell", "dash"), (lang, items)
    assert "capital" not in " ".join(items).lower()  # lowercase: no "capital X"


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
            screen = wifi_voice.instructions(dict(st, **extra), t)
            assert screen.strip() and screen != text, (code, screen)
            assert lang == "en" or "screen" not in screen, (code, screen)   # translated
            assert wifi_voice.instructions(dict(st, **extra), t, spelled=True) == text
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
