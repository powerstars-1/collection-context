"""Independent backup/restore CLI: python -m collection_context.library.backup_cli."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from collection_context.application.contracts import ContextError, envelope
from collection_context.library.backup import create_backup, restore_backup
from collection_context.library.store import LibraryStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="收藏上下文资料库一致备份与异路径恢复")
    commands = result.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="备份已提交资料；不包含密钥、登录态或索引暂存")
    backup.add_argument("--workspace", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--media", choices=("all", "none"), required=True)
    restore = commands.add_parser("restore", help="先完整验证，再恢复到新路径或空目录")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    store: LibraryStore | None = None
    try:
        if args.command == "backup":
            store = LibraryStore(args.workspace)
            data = create_backup(store, args.output, media_scope=args.media)
        else:
            data = restore_backup(args.archive, args.destination)
        print(json.dumps(envelope(data), ensure_ascii=False))
        return 0
    except ContextError as error:
        print(json.dumps(envelope(error=error), ensure_ascii=False))
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
