#
# common_words.py — the words that say nothing about an answer (owed.py, #203).
#
# owed.py asks its judges about a reply only when it is ABOUT an owed record
# (owed.Record.about: a word shared with its request, the caller's words or its result's
# TERMS, all outside this list), and only such a reply counts toward the backstop that
# closes a record told twice (#210). So this list is what keeps a reply that tells
# nothing from spending a judge call or the backstop: "Sure, take your time." meets an
# outcome that has "time" in it on "time" alone. The sync answers read the same terms
# for caller words "of their own" (owed.Owed.noted).
#
# What belongs here: function words, pronouns, auxiliaries and their forms, the
# conversational fillers a deferral or a waiting line is made of, generic time and
# quantity words, and the scaffolding a consult answer is written in ("top story",
# "score", "comments", "posted", "source", "link"). What does NOT: names, places,
# numbers (they are terms: one number is one numeric token, owed.spoken_tokens), and
# the polarity words "yes", "no", "not" -- a yes/no answer is otherwise term-less and
# only a dedicated run could close it (redteam-gist #5).
#
# A miss here errs in one direction only: a common word left out becomes a term, and a
# filler sharing it with a record is judged (the judge says OWED) and could spend the
# backstop. A word added in error can leave a telling about nothing -- owed, which costs
# a mention, never a silence (DESIGN §3).
# Inflections are listed where the lookup's suffix fold (owed._is_common) would not reach
# them.
#
# Short lists for the other voices' languages (engine_tts.LANG_NAMES): their function
# words, and for the scripts written without spaces (each character is a token) the
# commonest particles. Thin on purpose: a session in those languages errs owed.

_EN = """
a about above according across actually after afternoon again against ago ah ahead
alright all almost alone along already also although always am among an and another
answer answered answers any anybody anymore anyone anything anyway anyways anywhere
apparently are area aren around article articles as ask asked asking at available
away back bad be became because become been before began begin behind being believe
below best better between big bit both bottom brief briefly bring brought but buy by
call called came can cannot cant case certain certainly change check checked
checking clear close come comes coming comment comments complete completed could
couldn count course current currently day days dear deeper detail details did didn different
dig do does doesn
doing don done down during each earlier early easy either else end enough entire
especially etc even evening ever every everybody everyone everything exactly example
except far feel felt few find finding finds fine finished first for found from front
full further get gets getting give given gives giving glad go goes going gone good
got gotten great guess guy guys had hadn half hang happen happened happens hard has hasn have
haven having he hear heard hearing hello help her here hers herself hey hi high him
himself his hm hmm hold honestly hope how however huh i if im important in indeed
info information instead interesting into is isn it its itself just keep kept kind
know known knows last late later latest least left less let lets like likely line
link links list listed little live long look looked looking looks lot lots low made
main make makes making many matter may maybe me mean means meanwhile mention
mentioned might mind mine minute minutes moment more morning most mostly much must my
myself name named near nearly need needed needs never new news next nice night
nobody none nope nor nothing now nowhere of off often oh ok okay old on once one
ones only onto or other others otherwise our ours ourselves out over overall own page
part particular past people per perhaps place please plus point points possible post
posted posts pretty probably problem put quick quickly quite rank ranked rather read ready real
really reason recent recently recheck rechecked regarding remember report
reported reports request
result results right rush said same saw say saying says score scored scores search searched
searches searching second seconds see seem
seemed seems seen sent set several shall she short should shouldn show showed shown
shows side similar simply since site so some somebody someone something sometime
sometimes somewhat somewhere soon sorry sort source sources specific start started
still stories story stuff such summary super sure take taken takes taking talk
talking tell telling tells than thank thanks that thats the their theirs them
themselves then there therefore these they thing things think this those though
thought through thus time times title titled to today together told too took top
toward towards tried true try trying turn turned type uh um under unless until up
upon us use used using usual usually very via vote votes upvote upvotes wait waiting want wanted wants was
wasn way ways we website week weeks well went were weren what whatever when whenever
where whether which while who whoever whole whom whose why will wish with within
without won wonder wondering word words work worked working works would wouldn wow
yeah year years yep yesterday yet you your yours yourself yourselves
continue continuing pause understand understood
"""

# Spanish, French, Italian, Portuguese, each in two parts. The first is the words a
# phrase cannot end on (articles, prepositions, conjunctions, determiners, unstressed
# pronouns): tools._status_phrase reads a status phrase ending on one as stopping
# mid-thought ("buscando vuelos a", "cercando nel"). The second is the rest: auxiliaries,
# adverbs, the fillers of a deferral, words that end a phrase well ("comprobando el
# tiempo", "controllando adesso"). owed reads both, as one list.
_ES_CUT = """
el la los las un una unos unas de del al a y o pero que en con por para sin sobre
hacia hasta desde entre durante según tras esta este estos estas ese esa su sus lo
le les me te se mi tu nos si
"""
_ES_REST = """
es son era fue ser estar esto eso hay muy mas más ya no sí ahora aquí bueno vale claro
gracias tiempo espera esperar listo hola momento
"""
_FR_CUT = """
le la les un une des de du et ou mais que qui en dans avec par pour sans sur ce cette
ces cet se sa son ses leur leurs ne au aux si
"""
_FR_REST = """
est sont était être avoir a ont il elle ils elles on je tu nous vous pas plus très
déjà oui non bien merci maintenant voilà bon d'accord attendre attends prêt moment
temps
"""
_IT_CUT = """
il lo la i gli le un una uno di del della dei e o ma che in con per su da questa
questi quella se si mi ti ci vi ne al alla a ai agli alle allo dello delle degli nel
nella nei nelle sul sulla sui sulle dal dalla dai dalle tra fra
"""
_IT_REST = """
è sono era essere avere ha hanno questo quello non più molto già sì ora adesso bene
grazie aspetta pronto momento tempo
"""
_PT_CUT = """
o a os as um uma uns umas de do da dos das e ou mas que em no na nos nas com por para
sem sobre este esta esse essa seu sua me te lhe se
"""
_PT_REST = """
é são era ser estar está isto isso há muito mais já não sim agora aqui bem obrigado
obrigada espera pronto momento tempo
"""
# Scripts without spaces: one token per character (owed.spoken_tokens).
_CJK = "的了是在我你他她它们这那有不就也都和吗呢吧啊个一上下说好要会对着没很还想"
_JA = "のはがをにでとへもやかなねよだですますたいるうえおこそあ"

# The words a status phrase cannot end on, by catalog language (tools._status_phrase;
# English has its own finer test there, tools._dangling).
BY_LANGUAGE: dict[str, frozenset] = {
    lang: frozenset(block.split())
    for lang, block in (("es", _ES_CUT), ("fr", _FR_CUT), ("it", _IT_CUT), ("pt_BR", _PT_CUT))}

COMMON: frozenset = frozenset(
    w for block in (_EN, _ES_CUT, _ES_REST, _FR_CUT, _FR_REST, _IT_CUT, _IT_REST, _PT_CUT,
                    _PT_REST) for w in block.split()
) | frozenset(_CJK) | frozenset(_JA)
