import random

from services.name_matcher import match_name


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
    if not playlists:
        return None, None

    ids_by_key = {}
    for key, playlist_value in playlists.items():
        playlist_ids = _playlist_ids(playlist_value)
        if playlist_ids:
            ids_by_key[key] = playlist_ids

    matched_key = match_name(playlist_hint, {key: [key] for key in ids_by_key})
    if not matched_key:
        return None, None

    return matched_key, chooser(ids_by_key[matched_key])
