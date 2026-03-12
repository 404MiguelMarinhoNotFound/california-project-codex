import random


def _normalize_text(value: str) -> str:
    cleaned = " ".join((value or "").lower().split())
    return "".join(ch for ch in cleaned if ch.isalnum() or ch.isspace()).strip()


def _playlist_ids(value) -> list[str]:
    if isinstance(value, str):
        item = value.strip()
        return [item] if item else []

    if isinstance(value, (list, tuple, set)):
        ids = []
        for item in value:
            if isinstance(item, str) and item.strip():
                ids.append(item.strip())
        return ids

    return []


def resolve_playlist_choice(
    playlist_hint: str,
    playlists: dict,
    chooser=random.choice,
) -> tuple[str | None, str | None]:
    hint = _normalize_text(playlist_hint)
    if not hint or not playlists:
        return None, None

    normalized_entries = []
    for key, playlist_value in playlists.items():
        playlist_ids = _playlist_ids(playlist_value)
        if not playlist_ids:
            continue
        normalized_entries.append((key, _normalize_text(key), playlist_ids))

    for key, normalized_key, playlist_ids in normalized_entries:
        if normalized_key == hint:
            return key, chooser(playlist_ids)

    for key, normalized_key, playlist_ids in normalized_entries:
        if hint in normalized_key or normalized_key in hint:
            return key, chooser(playlist_ids)

    hint_tokens = set(hint.split())
    best_match = (None, None, 0.0)
    for key, normalized_key, playlist_ids in normalized_entries:
        key_tokens = set(normalized_key.split())
        if not key_tokens:
            continue
        score = len(hint_tokens & key_tokens) / len(key_tokens)
        if score > best_match[2]:
            best_match = (key, chooser(playlist_ids), score)

    if best_match[2] >= 0.5:
        return best_match[0], best_match[1]

    return None, None
