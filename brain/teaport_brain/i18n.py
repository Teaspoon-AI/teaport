#
# teaport — translations for what the brain says and shows on its own, without the LLM
# (today: Wi-Fi setup, wifi_voice.py and wifi_setup.py). The LLM already answers in the
# session's language; text the brain composes itself has to be translated here.
#
# Python's standard gettext — the machinery Django's _() wraps — over one catalog per
# language: teaport_brain/locale/<lang>/LC_MESSAGES/teaport.po is the editable source
# (any PO editor, e.g. Poedit), and the .mo beside it is what gettext loads, compiled
# from it by this module:
#
#     python -m teaport_brain.i18n          # recompile every .mo from its .po
#     python -m teaport_brain.i18n --check  # exit 1 if a committed .mo is stale
#
# (tests/test_i18n.py runs the check, the same way docs/CONFIG.md is held to its schema.)
#
# The language of a session is its voice's: the engine's TTS language (engine_tts.py,
# TTS_VOICE / TTS_LANGUAGE / OpenClaw's per-session choice / switch_voice), mapped to a
# catalog by ESPEAK_TO_LANG. A page in a phone's hands follows the phone's
# Accept-Language instead. English is the source language: its strings are the msgids,
# and a missing translation falls back to them.
#
import functools
import gettext
import os
import re
import struct
import sys

DOMAIN = "teaport"
LOCALE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locale")
# The engine's TTS languages (engine_tts._PREFIX_ESPEAK) -> catalog languages.
ESPEAK_TO_LANG = {
    "en-us": "en", "en-gb": "en", "es": "es", "fr-fr": "fr", "it": "it",
    "pt-br": "pt_BR", "hi": "hi", "ja": "ja", "cmn": "zh",
}
SOURCE_LANG = "en"
# Catalogs written right to left (the page sets dir="rtl" for them).
RTL = frozenset({"ar"})


def N_(msgid: str) -> str:
    """Mark a string for translation without translating it here (gettext's N_ idiom):
    it travels as English — a failure reason in status.json, a form error — and the
    reader puts it in their language with T._() at the point of display."""
    return msgid


class T:
    """One language's strings: T._("text"), T.p("context", "text")."""

    def __init__(self, lang: str):
        self.lang = lang
        self._t = gettext.translation(DOMAIN, LOCALE_DIR, languages=[lang], fallback=True)

    def _(self, msgid: str) -> str:
        return self._t.gettext(msgid)

    def p(self, context: str, msgid: str) -> str:
        return self._t.pgettext(context, msgid)

    @property
    def dir(self) -> str:
        return "rtl" if self.lang in RTL else "ltr"


@functools.cache
def get(lang: str) -> T:
    return T(lang if lang in catalogs() or lang == SOURCE_LANG else SOURCE_LANG)


def for_espeak(code: str | None) -> T:
    """The strings for a session whose TTS speaks `code` ('en-us', 'es', 'cmn', ...)."""
    return get(ESPEAK_TO_LANG.get((code or "").lower(), SOURCE_LANG))


def for_accept_language(header: str | None) -> T:
    """The best catalog for a browser's Accept-Language ('es-MX,es;q=0.9,en;q=0.8')."""
    choices = []
    for i, part in enumerate((header or "").split(",")):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        m = re.search(r"q=([0-9.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if tag and q > 0:
            choices.append((-q, i, tag.strip().replace("-", "_")))
    have = set(catalogs()) | {SOURCE_LANG}
    for _, _, tag in sorted(choices):
        lang, _, region = tag.partition("_")
        for candidate in (f"{lang.lower()}_{region.upper()}" if region else "", lang.lower()):
            if candidate in have:
                return get(candidate)
        if lang.lower() == "pt" and "pt_BR" in have:
            return get("pt_BR")  # the one Portuguese the voice speaks
    return get(SOURCE_LANG)


@functools.cache
def catalogs() -> tuple[str, ...]:
    try:
        return tuple(sorted(d for d in os.listdir(LOCALE_DIR)
                            if os.path.exists(_po_path(d))))
    except OSError:
        return ()


def _po_path(lang: str) -> str:
    return os.path.join(LOCALE_DIR, lang, "LC_MESSAGES", f"{DOMAIN}.po")


# ------------------------------------------------------------------ .po -> .mo
# A msgfmt for the subset these catalogs use: msgctxt/msgid/msgstr, multi-line strings,
# comments, the usual escapes; no plurals. Deterministic, so a stale .mo is detectable.

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unquote(s: str) -> str:
    s = s.strip()
    if not (s.startswith('"') and s.endswith('"')):
        raise ValueError(f"not a quoted string: {s!r}")
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), s[1:-1])


def parse_po(text: str) -> dict[str, str]:
    """{key: msgstr}; key is msgid, or msgctxt + '\\x04' + msgid (gettext's convention).
    Untranslated entries (empty msgstr) are left out, so they fall back to English."""
    entries: dict[str, str] = {}
    cur: dict[str, str] = {}
    field = None

    def flush():
        if "msgid" in cur and cur.get("msgstr"):
            key = cur["msgid"] if "msgctxt" not in cur else cur["msgctxt"] + "\x04" + cur["msgid"]
            entries[key] = cur["msgstr"]
        cur.clear()

    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            if not line and cur.get("msgstr") is not None:
                flush()
                field = None
            continue
        m = re.match(r"(msgctxt|msgid|msgstr)\s+(\".*\")$", line)
        try:
            if m:
                if m.group(1) in ("msgctxt", "msgid") and "msgstr" in cur:
                    flush()
                field = m.group(1)
                cur[field] = _unquote(m.group(2))
            elif line.startswith('"') and field:
                cur[field] += _unquote(line)
            else:
                raise ValueError(f"unexpected line: {raw!r}")
        except ValueError as e:
            raise ValueError(f"line {n}: {e}") from None
    flush()
    return entries


def compile_mo(entries: dict[str, str]) -> bytes:
    """GNU .mo bytes for `entries` (keys sorted, no hash table — gettext does not need one)."""
    keys = sorted(entries, key=lambda k: k.encode())
    ids = b""
    strs = b""
    offsets = []
    for k in keys:
        kb, vb = k.encode(), entries[k].encode()
        offsets.append((len(ids), len(kb), len(strs), len(vb)))
        ids += kb + b"\0"
        strs += vb + b"\0"
    n = len(keys)
    keystart = 7 * 4 + 16 * n
    valuestart = keystart + len(ids)
    koffsets, voffsets = [], []
    for o1, l1, o2, l2 in offsets:
        koffsets += [l1, o1 + keystart]
        voffsets += [l2, o2 + valuestart]
    header = struct.pack("Iiiiiii", 0x950412DE, 0, n, 7 * 4, 7 * 4 + n * 8, 0, 0)
    return header + struct.pack(f"{len(koffsets)}i", *koffsets) + \
        struct.pack(f"{len(voffsets)}i", *voffsets) + ids + strs


def compiled(lang: str) -> bytes:
    with open(_po_path(lang), encoding="utf-8") as f:
        return compile_mo(parse_po(f.read()))


def main(argv=None) -> int:
    check = "--check" in (argv if argv is not None else sys.argv[1:])
    stale = []
    for lang in catalogs():
        mo = _po_path(lang)[:-3] + ".mo"
        data = compiled(lang)
        try:
            with open(mo, "rb") as f:
                current = f.read()
        except OSError:
            current = None
        if current == data:
            continue
        if check:
            stale.append(lang)
        else:
            with open(mo, "wb") as f:
                f.write(data)
            print(f"{mo}: compiled")
    if stale:
        print(f"stale .mo for {', '.join(stale)} — run: python -m teaport_brain.i18n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
