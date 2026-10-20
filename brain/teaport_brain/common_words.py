#
# common_words.py — the words that say nothing about an answer (owed.py, #203).
#
# owed.py closes an owed record only when the caller heard at least min(2, |terms|) of
# each item's TERMS, and an item's terms are its spoken tokens outside this list (and
# outside the request's own tokens). So this list is what keeps a reply that tells
# nothing from closing a record: "Sure, take your time." meets an outcome that has
# "time" in it on "time" alone, and the 2-term minimum alone does not stop a filler
# made of two common words (formal/Owed.tla, ow_singleWord).
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
# filler sharing TWO of them with an item could close it. A word added in error makes an
# item harder to close -- owed, which costs a mention, never a silence (DESIGN §3).
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
couldn count course current currently day days dear did didn different do does doesn
doing don done down during each earlier early easy either else end enough entire
especially etc even evening ever every everybody everyone everything exactly example
except far feel felt few find finding finds fine finished first for found from front
full further get gets getting give given gives giving glad go goes going gone good
got gotten great guess had hadn half hang happen happened happens hard has hasn have
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
really reason recent recently regarding remember report reported reports request
result results right rush said same saw say saying says score scored scores second seconds see seem
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

# Spanish, French, Italian, Portuguese: articles, prepositions, pronouns, auxiliaries,
# the fillers of a deferral.
_ES = """
el la los las un una unos unas de del al y o pero que en con por para sin sobre es
son era fue ser estar esta este esto estos estas eso ese esa hay muy mas más ya no
sí si se su sus lo le les me te mi tu nos ahora aquí bueno vale claro gracias
tiempo espera esperar listo hola momento
"""
_FR = """
le la les un une des de du et ou mais que qui en dans avec par pour sans sur est
sont était être avoir a ont ce cette ces cet il elle ils elles on je tu nous vous
se sa son ses leur leurs ne pas plus très déjà oui non si au aux bien merci
maintenant voilà bon d'accord attendre attends prêt moment temps
"""
_IT = """
il lo la i gli le un una uno di del della dei e o ma che in con per su da è sono
era essere avere ha hanno questo questa questi quello quella non più molto già sì
se si mi ti ci vi ne al alla ora adesso bene grazie aspetta pronto momento tempo
"""
_PT = """
o a os as um uma uns umas de do da dos das e ou mas que em no na nos nas com por
para sem sobre é são era ser estar está este esta isto esse essa isso há muito mais
já não sim se seu sua me te lhe agora aqui bem obrigado obrigada espera pronto
momento tempo
"""
# Scripts without spaces: one token per character (owed.spoken_tokens).
_CJK = "的了是在我你他她它们这那有不就也都和吗呢吧啊个一上下说好要会对着没很还想"
_JA = "のはがをにでとへもやかなねよだですますたいるうえおこそあ"

COMMON: frozenset = frozenset(
    w for block in (_EN, _ES, _FR, _IT, _PT) for w in block.split()
) | frozenset(_CJK) | frozenset(_JA)
