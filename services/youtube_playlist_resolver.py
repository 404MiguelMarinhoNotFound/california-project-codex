import random

from services.name_matcher import match_name


def playlist_ids(value) -> list[str]:
    """Normalize one config entry (str or list) into a list of usable IDs.

    Public because the system-prompt inventory in services/llm.py has to
    advertise exactly the categories this resolver would accept.
    """
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


def playlist_aliases(aliases: dict | None, key: str) -> list[str]:
    """
    Extra spoken forms configured for one category, from the top-level
    `youtube_playlist_aliases` map. Never includes the key itself — match_name
    is always given the key as its own first candidate.

    Kept separate from `youtube_playlists` on purpose: that block is a plain
    key -> IDs map with four readers, two of which (the validator and the e2e
    tool) hand-roll the parse and would quietly skip or crash on a nested shape.
    """
    if not aliases:
        return []
    return [item.strip() for item in (aliases.get(key) or []) if isinstance(item, str) and item.strip()]


def resolve_playlist_choice(
    playlist_hint: str,
    playlists: dict,
    aliases: dict | None = None,
    chooser=random.choice,
) -> tuple[str | None, str | None]:
    if not playlists:
        return None, None

    ids_by_key = {}
    for key, playlist_value in playlists.items():
        ids = playlist_ids(playlist_value)
        if ids:
            ids_by_key[key] = ids

    # An alias naming a missing or empty category is inert: ids_by_key gates it.
    candidates = {key: [key, *playlist_aliases(aliases, key)] for key in ids_by_key}
    matched_key = match_name(playlist_hint, candidates)
    if not matched_key:
        return None, None

    return matched_key, chooser(ids_by_key[matched_key])
