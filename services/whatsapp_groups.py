"""
Matching a spoken group name against the cached list of WhatsApp group titles.

Contacts go through `score_contacts`, which is tuned for people's names typed
into a phone book. Group titles are a different shape, and the difference is
the whole reason this module exists. Measured on the real account 2026-10-02:

- **They are decorated.** 60 of 217 titles carry emoji, and some carry symbols
  as well: "🦩‍🔥☭flamingus unanounymous☭🦩‍🔥". Nothing spoken can match those
  characters, so they are dropped before any comparison.
- **They are misspelt on purpose and in other languages**, and Whisper then
  mishears them a second time. "flamingus unanounymous" came back from speech
  as "Flamingist" and "Anonymous group" -- neither a substring of the title, and
  two separate guesses at one name. So words are compared by spelling AND by a
  rough sound key, and the best title word for each spoken word counts.
- **They contain the word "group" or nothing like it.** "group", "grupo" and
  "chat" are what Master Miguel says to mean "a group", not part of the name,
  so they are dropped from both sides.

A group match is never certain. A message to a group reaches everyone in it,
and a fuzzy matcher is by definition sometimes wrong, so every group send is
read back first (see WhatsAppService.send_group). The matcher's job is to find
the right title often enough that the read-back is a "yes", and to say "which
one?" rather than pick when two titles score alike.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Words that say "a group" rather than which one. Dropped from the spoken name
# and from titles alike, so "the hiking group" and "Hiking" score as the same.
_FILLER = {
    "group", "groups", "grupo", "grupos", "chat", "chats", "gc",
    "the", "my", "our", "a", "an", "o", "os", "as", "de", "da", "do", "dos", "das",
}

# A title at or above this is worth offering at all. 0.62 let pure noise through
# ("xyz nonsense" scored 0.63 against three real titles); the real mishearings
# measured live score 0.79+.
MIN_SCORE = 0.70
# Titles within this of the best are a tie, and a tie is a question.
TIE_MARGIN = 0.08
# At most this many titles are offered in "which one?".
MAX_CANDIDATES = 3


@dataclass
class GroupMatch:
    """
    The outcome of resolving a spoken group name.

    `title` is set when one title clearly won; `candidates` when several were
    too close to pick between, and then `title` is empty. Falsy when nothing
    usable was found -- the same `__bool__` trap as every other result here,
    so check `candidates` before truthiness.
    """

    title: str = ""
    score: float = 0.0
    candidates: list[str] = field(default_factory=list)
    shared: bool = False  # the winning title belongs to more than one group

    def __bool__(self) -> bool:
        return bool(self.title)


def words(text: str) -> list[str]:
    """Lowercase ASCII words with accents folded and emoji, symbols and filler dropped."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    tokens = re.sub(r"[^a-z0-9]+", " ", plain.casefold()).split()
    kept = [t for t in tokens if t not in _FILLER]
    # A title that is nothing but filler ("Group") keeps its words rather than vanishing.
    return kept or tokens


def sound_key(word: str) -> str:
    """
    A rough sound-alike key: what survives when spelling stops mattering.

    Not Soundex -- that keeps only four characters, too few to tell long
    made-up words apart. Common spellings of one sound are folded together,
    vowels after the first letter are dropped, and a leading vowel becomes one
    marker, so "anonymous" and "unanounymous" both come out as "Anms".
    """
    w = word
    for a, b in (("ph", "f"), ("ck", "k"), ("qu", "k"), ("q", "k"), ("x", "ks"),
                 ("wh", "w"), ("kn", "n"), ("sch", "sk"), ("sh", "s"), ("ch", "s"),
                 ("z", "s"), ("w", "v"), ("y", "i")):
        w = w.replace(a, b)
    w = re.sub(r"c(?=[eij])", "s", w).replace("c", "k")
    if not w:
        return ""
    head = "A" if w[0] in "aeiou" else w[0]
    tail = re.sub(r"[aeiouh]", "", w[1:])
    return re.sub(r"(.)\1+", r"\1", head + tail)


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _word_similarity(spoken: str, written: str) -> float:
    if spoken == written:
        return 1.0
    # Two letters is too little to fuzz: "dv" must not find "da" or "tv".
    if len(spoken) <= 2 or len(written) <= 2:
        return 0.0
    return max(_ratio(spoken, written), 0.9 * _ratio(sound_key(spoken), sound_key(written)))


def score(spoken: list[str], title: list[str]) -> float:
    """
    How well a spoken name fits one title, 0 to 1.

    Mostly "did every spoken word find a title word" (Whisper keeps the words,
    just not their spelling), partly "is the title mostly accounted for" so a
    long title that merely contains one spoken word ranks below one that is
    just that word. The whole name run together is compared too, because
    speech splits and joins words freely ("party animals" / "partyanimals").
    """
    if not spoken or not title:
        return 0.0
    if spoken == title:
        return 1.0
    covered = sum(max(_word_similarity(s, t) for t in title) for s in spoken) / len(spoken)
    accounted = sum(max(_word_similarity(s, t) for s in spoken) for t in title) / len(title)
    by_words = 0.75 * covered + 0.25 * accounted
    run_together = _ratio("".join(spoken), "".join(title))
    return max(by_words, run_together)


def match_group(hint: str, titles: list[str]) -> GroupMatch:
    """Resolve a spoken group name against the cached titles."""
    spoken = words(hint)
    if not spoken or not titles:
        return GroupMatch()
    ranked = sorted(
        ((score(spoken, words(title)), title) for title in dict.fromkeys(titles)),
        key=lambda pair: -pair[0],
    )
    best_score, best = ranked[0]
    if best_score < MIN_SCORE:
        return GroupMatch()
    tied = [t for s, t in ranked if s >= best_score - TIE_MARGIN and s >= MIN_SCORE]
    if len(tied) > 1 and best_score < 1.0:
        return GroupMatch(candidates=tied[:MAX_CANDIDATES])
    return GroupMatch(title=best, score=best_score, shared=titles.count(best) > 1)


def speakable_title(title: str) -> str:
    """
    The title as something she can say: emoji, symbols and joiners removed.

    "🦩‍🔥☭flamingus unanounymous☭🦩‍🔥" becomes "flamingus unanounymous". Falls
    back to the raw title only when nothing speakable is left.
    """
    kept = "".join(" " if unicodedata.category(ch)[0] in "SC" else ch for ch in title or "")
    return " ".join(kept.split()) or (title or "")
