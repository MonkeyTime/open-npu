"""Copie SQLite cohérente, y compris pendant les écritures de l’instance."""

import argparse
from contextlib import closing
from pathlib import Path
import sqlite3


def verified_copy(source, destination):
    source = source.resolve(strict=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        original.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()
        with destination.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(destination)) as copy:
                original.backup(copy)
                if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("L’intégrité de la copie SQLite n’a pas pu être vérifiée.")
                if copy.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("La copie contient des références invalides.")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    verified_copy(args.source, args.destination)
    print(f"Copie SQLite vérifiée : {args.destination}")


if __name__ == "__main__":
    main()
