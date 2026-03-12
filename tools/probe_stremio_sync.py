import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.stremio_service import StremioService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", help="Optional title filter to inspect a specific cached entry")
    args = parser.parse_args()

    load_dotenv()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    service = StremioService(config)
    state = service._load_watch_state()

    payload = {
        "count": len(state),
        "sample_titles": sorted(entry.get("title", key) for key, entry in state.items())[:10],
    }

    if args.title:
        title_lower = args.title.lower()
        matches = [
            {"key": key, **entry}
            for key, entry in state.items()
            if title_lower in key.lower() or title_lower in str(entry.get("title", "")).lower()
        ]
        payload["matches"] = matches

    print(json.dumps(payload, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
