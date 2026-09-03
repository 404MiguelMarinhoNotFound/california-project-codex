"""
Shared fuzzy name matching for spoken requests.

Turns a loosely spoken hint ("beach samba", "the loft") into one of a set of
known keys. Four tiers, most confident first:

  1. exact match on the normalized text
  2. exact match once all spaces are removed ("road trip" -> "roadtrip")
  3. substring match in either direction
  4. token overlap, accepted only at 50% or better

Extracted from services/youtube_playlist_resolver.py so light room names can
reuse the same cascade. Iteration follows dict order, so earlier keys win ties.

**Tier 2 must stay above tier 3.** Despacing is what lets "warmwhite" reach
"warm white" instead of the substring tier finding "white" inside it and
returning the wrong colour. services/govee_service.py::resolve_color has always
hand-built despaced aliases for exactly this reason; tier 2 generalizes it.
See tests/test_name_matcher.py, which locks the ordering.
"""

TOKEN_MATCH_THRESHOLD = 0.5


def normalize_text(value: str) -> str:
    # "&" becomes " and " before the non-alphanumeric strip, so "R&B" reads as
    # "r and b" rather than collapsing to "rb". That matters: "rb" is a
    # two-character substring living inside "herbie", "urban" and "superb",
    # so the substring tier would match it far too eagerly.
    cleaned = " ".join((value or "").replace("&", " and ").lower().split())
    return "".join(ch for ch in cleaned if ch.isalnum() or ch.isspace()).strip()


def match_name(hint: str, candidates: dict) -> str | None:
    """
    Resolve a spoken hint to one key.

    `candidates` maps a key to the list of names that should resolve to it.
    The key itself is not matched implicitly, so include it in its own list.

    Returns the matched key, or None when nothing resolves confidently.
    """
    normalized_hint = normalize_text(hint)
    if not normalized_hint or not candidates:
        return None

    entries = []
    for key, names in candidates.items():
        for name in names:
            normalized_name = normalize_text(name)
            if normalized_name:
                entries.append((key, normalized_name))

    for key, normalized_name in entries:
        if normalized_name == normalized_hint:
            return key

    despaced_hint = normalized_hint.replace(" ", "")
    for key, normalized_name in entries:
        if normalized_name.replace(" ", "") == despaced_hint:
            return key

    for key, normalized_name in entries:
        if normalized_hint in normalized_name or normalized_name in normalized_hint:
            return key

    hint_tokens = set(normalized_hint.split())
    best_key = None
    best_score = 0.0
    for key, normalized_name in entries:
        name_tokens = set(normalized_name.split())
        if not name_tokens:
            continue
        score = len(hint_tokens & name_tokens) / len(name_tokens)
        if score > best_score:
            best_key = key
            best_score = score

    return best_key if best_score >= TOKEN_MATCH_THRESHOLD else None
