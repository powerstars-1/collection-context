"""Independent browser smoke check; credentials/profile are separate from the vault."""

import argparse
import json
from pathlib import Path

from collection_context.application.contracts import ContextError
from collection_context.infrastructure.browser import BrowserSession


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args()
    try:
        with BrowserSession(args.profile, headless=True) as browser:
            print(json.dumps(browser.probe_public_site(), ensure_ascii=False, indent=2))
    except ContextError as error:
        print(json.dumps({"ok": False, "error": error.as_dict()}, ensure_ascii=False, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
