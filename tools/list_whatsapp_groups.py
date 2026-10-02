"""
List every WhatsApp group this account is in, and refresh the cached list.

Reads WhatsApp Web's chat list under the "Groups" filter, scrolled to the end,
through the same WhatsAppService the assistant uses, so it writes the same
cache (`whatsapp.groups_path`, gitignored). Opens no chat, so nothing is marked
read and nobody sees anything.

    uv run python tools/list_whatsapp_groups.py
    uv run python tools/list_whatsapp_groups.py --cached   # print the cache, no browser

Stop California first: her browser holds the WhatsApp profile, and a second
browser cannot open a profile that is in use.
"""

import argparse
import collections
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.whatsapp_service import WhatsAppService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="List California's WhatsApp groups.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--cached", action="store_true", help="print the cached list without a browser")
    args = parser.parse_args()
    # Group titles are full of emoji; the Windows console default (cp1252) is not.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    service = WhatsAppService(config)
    try:
        if not args.cached:
            result = service.list_groups()
            if not result:
                print(result.message)
                print("Is California still running? Her browser holds the profile.")
                return 1
        groups = service.groups
        if not groups:
            print(f"No groups cached at {service.groups_path}.")
            return 1
        for g in groups:
            print(f"  {g['name']}{'  (muted)' if g.get('muted') else ''}")
        dupes = [n for n, c in collections.Counter(g["name"].casefold() for g in groups).items() if c > 1]
        print(f"\n{len(groups)} group(s), {sum(1 for g in groups if g.get('muted'))} muted -> {service.groups_path}")
        if dupes:
            print(f"Shared titles (a send by name would have to ask which): {', '.join(dupes)}")
        return 0
    finally:
        service.close()


if __name__ == "__main__":
    sys.exit(main())
