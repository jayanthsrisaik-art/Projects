from winstream.engine import Operator
from winstream.model import Event


def ev(eid, key, ts, value=1, arrival=None):
    return Event(eid, key, ts, value, arrival)


def drive(spec, delay, lateness, events, flush=True):
    op = Operator(spec, delay, lateness)
    out, side = [], []
    for i, e in enumerate(events):
        o, s = op.process(e, i)
        out.extend(o)
        side.extend(s)
    if flush:
        out.extend(op.flush())
    return op, out, side


def materialise(emissions):
    final = {}
    for r in emissions:
        k = (r["key"], r["wstart"], r["wend"])
        if r["kind"] == "retract":
            del final[k]
        else:
            final[k] = {c: r[c] for c in ("count", "sum", "min", "max", "mean")}
    return final
