import json


def load(path):
    with open(path) as fh:
        return json.load(fh)


def total(items):
    """Sum price * qty over the cart."""
    result = 0
    for item in items:
        result += item["price"] * item.get("qty", 1)
    return result


def main():
    data = load("cart.json")
    print(total(data))
