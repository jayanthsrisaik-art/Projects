import json
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def total(items):
    result = 0
    for item in items:
        result += item["price"]
    return result


def main(argv=None):
    argv = argv or sys.argv[1:]
    data = load(argv[0] if argv else "cart.json")
    print(total(data))
