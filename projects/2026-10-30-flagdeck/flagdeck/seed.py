"""Load flags from a JSON file into a store, in prerequisite order, skipping ones that exist."""
import json

from .schema import FlagIn
from .store import NotFound, Store


def seed(db_path: str, path: str, actor: str = "seed") -> str:
    store = Store(db_path)
    with open(path, encoding="utf-8") as fh:
        docs = [FlagIn.model_validate(d).doc() for d in json.load(fh)]
    pending = {d["key"]: d for d in docs}
    created, skipped = [], []
    while pending:
        ready = [k for k, d in pending.items()
                 if all(p["key"] not in pending or p["key"] == k for p in d["prerequisites"])]
        if not ready:
            raise ValueError(f"prerequisite cycle among {sorted(pending)}")
        for k in sorted(ready):
            doc = pending.pop(k)
            try:
                store.get(k)
                skipped.append(k)
            except NotFound:
                store.create(doc, actor, "seeded")
                created.append(k)
    return f"seeded {len(created)} flag(s), {len(skipped)} already present"
