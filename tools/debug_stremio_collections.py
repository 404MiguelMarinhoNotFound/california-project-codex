import json
import os
from pathlib import Path
from collections import Counter

import requests
import yaml
from dotenv import load_dotenv


COLLECTIONS = [
    "libraryItem",
    "library",
    "historyItem",
    "history",
    "continueWatching",
]


def main():
    load_dotenv()
    config = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
    stremio_cfg = config.get("stremio", {})
    email = os.getenv("STREMIO_EMAIL") or stremio_cfg.get("email")
    password = os.getenv("STREMIO_PASSWORD") or stremio_cfg.get("password")
    if not email or not password:
        raise SystemExit("Missing Stremio credentials")

    login = requests.post(
        "https://api.strem.io/api/login",
        json={"email": email, "password": password, "type": "Login"},
        timeout=15,
    )
    login.raise_for_status()
    auth_key = login.json().get("result", {}).get("authKey")
    if not auth_key:
        raise SystemExit("Login succeeded but authKey was missing")

    summary = []
    for collection in COLLECTIONS:
        response = requests.post(
            "https://api.strem.io/api/datastoreGet",
            json={"authKey": auth_key, "collection": collection, "ids": [], "all": True},
            timeout=20,
        )
        payload = response.json()
        result = payload.get("result")
        first = result[0] if isinstance(result, list) and result else None
        summary.append(
            {
                "collection": collection,
                "status_code": response.status_code,
                "result_type": type(result).__name__,
                "count": len(result) if isinstance(result, list) else None,
                "type_counts": dict(Counter(item.get("type") for item in result)) if isinstance(result, list) else None,
                "sample_series": [
                    item
                    for item in result
                    if isinstance(item, dict) and item.get("type") in {"series", "movie", "tv"}
                ][:5] if isinstance(result, list) else None,
                "first_keys": list(first.keys())[:15] if isinstance(first, dict) else None,
                "first_item": first,
            }
        )

    print(json.dumps(summary, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
