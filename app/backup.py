"""Create a consistent SQLite snapshot while the controller may be running."""

import argparse
from pathlib import Path

from .db import Database


def main():
    parser = argparse.ArgumentParser(description="Back up Agent Controller state")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = args.data / "controller.sqlite"
    if not source.is_file():
        parser.error(f"controller database not found: {source}")
    database = Database(source)
    print(database.backup(args.out))


if __name__ == "__main__":
    main()
