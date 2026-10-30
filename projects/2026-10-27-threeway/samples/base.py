import json


def load(path):
    with open(path) as fh:
        return json.load(fh)


def total(items):
    result = 0
    for item in items:
        result += item["price"]
    return result


def main():
    data = load("cart.json")
    print(total(data))
