"""Local app entry: opening the app opens a picker, never initializes a library."""

import sys

from collection_context.native_bootstrap import dispatch

if __name__ == "__main__":
    raise SystemExit(dispatch(["desktop", *sys.argv[1:]]))
