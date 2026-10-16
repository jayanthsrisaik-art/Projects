"""CLI: python -m lsmkv.cli DIR run SCRIPT   (commands: put K V | get K | del K | scan [A [B]])"""
import sys

from .store import LSMStore


def run(db: LSMStore, lines, out) -> None:
    for line in lines:
        parts = line.split()
        if not parts or parts[0].startswith("#"):
            continue
        cmd, args = parts[0], [a.encode() for a in parts[1:]]
        if cmd == "put":
            db.put(args[0], args[1])
        elif cmd == "del":
            db.delete(args[0])
        elif cmd == "get":
            v = db.get(args[0])
            print(f"{args[0].decode()} = {v.decode() if v is not None else '(nil)'}", file=out)
        elif cmd == "scan":
            for k, v in db.scan(*(args[:2])):
                print(f"{k.decode()} = {v.decode()}", file=out)
        else:
            raise SystemExit(f"unknown command: {cmd}")


def main(argv=None) -> None:
    argv = argv or sys.argv[1:]
    if len(argv) != 3 or argv[1] != "run":
        raise SystemExit(__doc__)
    with LSMStore(argv[0]) as db, open(argv[2]) as f:
        run(db, f, sys.stdout)


if __name__ == "__main__":
    main()
