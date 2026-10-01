"""PyInstaller entry: no data directory or credential is selected implicitly."""

from collection_context.native_bootstrap import dispatch

if __name__ == "__main__":
    raise SystemExit(dispatch())
