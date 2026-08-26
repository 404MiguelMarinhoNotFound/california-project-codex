"""
Shared fuzzy name matching for spoken requests.

Turns a loosely spoken hint ("beach samba", "the loft") into one of a set of
known keys. Three tiers, most confident first:

  1. exact match on the normalized text
  2. substring match in either direction
  3. token overlap, accepted only at 50% or better

Extracted from services/youtube_playlist_resolver.py so light room names can
reuse the same cascade. Iteration follows dict order, so earlier keys win ties.
"""

TOKEN_MATCH_THRESHOLD = 0.5


def normalize_text(value: str) -> str:
    cleaned = " ".join((value or "").lower().split())
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
