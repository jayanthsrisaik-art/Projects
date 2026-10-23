"""Direction-aware breaking-change detection between two OpenAPI 3.x documents."""
from __future__ import annotations

from dataclasses import dataclass, asdict

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
BREAKING, WARNING, SAFE = "breaking", "warning", "safe"

REQUEST, RESPONSE = "request", "response"
# Numeric/length constraints: (keyword, direction in which a *larger* value is tighter)
_LIMITS = {"minimum": 1, "minLength": 1, "minItems": 1,
           "maximum": -1, "maxLength": -1, "maxItems": -1}


@dataclass(frozen=True)
class Change:
    severity: str
    code: str
    where: str
    message: str

    def to_dict(self):
        return asdict(self)


class Differ:
    def __init__(self, old: dict, new: dict):
        self.old, self.new = old, new
        self.changes: list[Change] = []

    # -- helpers ---------------------------------------------------------
    def _add(self, sev, code, where, msg):
        self.changes.append(Change(sev, code, where, msg))

    @staticmethod
    def _resolve(doc, node, seen=()):
        """Follow local $ref chains. Returns (node, ref_name_or_None)."""
        ref = None
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in seen or not ref.startswith("#/"):
                return {}, ref
            seen = seen + (ref,)
            cur = doc
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                if not isinstance(cur, dict) or part not in cur:
                    return {}, ref
                cur = cur[part]
            node = cur
        return node if isinstance(node, dict) else {}, ref

    # -- entry point -----------------------------------------------------
    def diff(self) -> list[Change]:
        old_paths = self.old.get("paths", {}) or {}
        new_paths = self.new.get("paths", {}) or {}
        for path in sorted(old_paths.keys() - new_paths.keys()):
            self._add(BREAKING, "path-removed", path, "path removed")
        for path in sorted(new_paths.keys() - old_paths.keys()):
            self._add(SAFE, "path-added", path, "path added")
        for path in sorted(old_paths.keys() & new_paths.keys()):
            self._diff_path(path, old_paths[path], new_paths[path])
        order = {BREAKING: 0, WARNING: 1, SAFE: 2}
        self.changes.sort(key=lambda c: (order[c.severity], c.where, c.code))
        return self.changes

    def _diff_path(self, path, o, n):
        o, _ = self._resolve(self.old, o)
        n, _ = self._resolve(self.new, n)
        for m in METHODS:
            where = f"{m.upper()} {path}"
            if m in o and m not in n:
                self._add(BREAKING, "operation-removed", where, "operation removed")
            elif m not in o and m in n:
                self._add(SAFE, "operation-added", where, "operation added")
            elif m in o and m in n:
                self._diff_operation(where, o, n, m)

    def _diff_operation(self, where, opath, npath, m):
        oo, no = opath[m], npath[m]
        if not oo.get("deprecated") and no.get("deprecated"):
            self._add(WARNING, "operation-deprecated", where, "operation deprecated")
        self._diff_params(where, self._params(self.old, opath, oo), self._params(self.new, npath, no))
        self._diff_body(where, oo.get("requestBody"), no.get("requestBody"))
        self._diff_responses(where, oo.get("responses", {}) or {}, no.get("responses", {}) or {})

    def _params(self, doc, path_item, op):
        out = {}
        for p in (path_item.get("parameters") or []) + (op.get("parameters") or []):
            p, _ = self._resolve(doc, p)
            if "name" in p and "in" in p:
                out[(p["in"], p["name"])] = p  # operation-level overrides path-level
        return out

    # -- parameters ------------------------------------------------------
    def _diff_params(self, where, op, np):
        for key in sorted(op.keys() - np.keys()):
            self._add(WARNING, "param-removed", f"{where} {key[0]}:{key[1]}",
                      "parameter removed (servers may now ignore or reject it)")
        for key in sorted(np.keys() - op.keys()):
            w = f"{where} {key[0]}:{key[1]}"
            if np[key].get("required"):
                self._add(BREAKING, "param-added-required", w, "new required parameter")
            else:
                self._add(SAFE, "param-added", w, "new optional parameter")
        for key in sorted(op.keys() & np.keys()):
            w = f"{where} {key[0]}:{key[1]}"
            if not op[key].get("required") and np[key].get("required"):
                self._add(BREAKING, "param-now-required", w, "parameter became required")
            elif op[key].get("required") and not np[key].get("required"):
                self._add(SAFE, "param-now-optional", w, "parameter became optional")
            self._schema(w, op[key].get("schema", {}), np[key].get("schema", {}), REQUEST, ())

    # -- bodies ----------------------------------------------------------
    def _diff_body(self, where, ob, nb):
        w = f"{where} requestBody"
        ob = self._resolve(self.old, ob or {})[0] if ob else None
        nb = self._resolve(self.new, nb or {})[0] if nb else None
        if ob is None and nb is None:
            return
        if ob is None:
            sev = BREAKING if nb.get("required") else SAFE
            self._add(sev, "body-added", w, "request body added" + (" (required)" if sev == BREAKING else ""))
            return
        if nb is None:
            self._add(WARNING, "body-removed", w, "request body removed")
            return
        if not ob.get("required") and nb.get("required"):
            self._add(BREAKING, "body-now-required", w, "request body became required")
        self._diff_content(w, ob.get("content", {}), nb.get("content", {}), REQUEST)

    def _diff_content(self, w, oc, nc, direction):
        for mt in sorted(oc.keys() - nc.keys()):
            sev = BREAKING
            self._add(sev, "media-type-removed", f"{w} [{mt}]", "media type removed")
        for mt in sorted(nc.keys() - oc.keys()):
            self._add(SAFE, "media-type-added", f"{w} [{mt}]", "media type added")
        for mt in sorted(oc.keys() & nc.keys()):
            self._schema(f"{w} [{mt}]", oc[mt].get("schema", {}), nc[mt].get("schema", {}), direction, ())

    def _diff_responses(self, where, orr, nr):
        for code in sorted(orr.keys() - nr.keys()):
            self._add(BREAKING, "response-removed", f"{where} -> {code}", "response status removed")
        for code in sorted(nr.keys() - orr.keys()):
            self._add(WARNING, "response-added", f"{where} -> {code}", "new response status (clients may not handle it)")
        for code in sorted(orr.keys() & nr.keys()):
            o = self._resolve(self.old, orr[code])[0]
            n = self._resolve(self.new, nr[code])[0]
            self._diff_content(f"{where} -> {code}", o.get("content", {}), n.get("content", {}), RESPONSE)

    # -- schemas ---------------------------------------------------------
    def _schema(self, w, o, n, d, seen):
        o, oref = self._resolve(self.old, o)
        n, nref = self._resolve(self.new, n)
        key = (oref, nref)
        if (oref or nref) and key in seen:
            return  # recursive schema already being compared
        seen = seen + (key,)
        if not o and not n:
            return
        ot, nt = o.get("type"), n.get("type")
        if ot != nt and (ot or nt):
            if d == REQUEST and ot == "integer" and nt == "number":
                self._add(SAFE, "type-widened", w, "integer -> number (request widened)")
            elif d == RESPONSE and ot == "integer" and nt == "number":
                self._add(BREAKING, "type-changed", w, "response integer -> number")
            else:
                self._add(BREAKING, "type-changed", w, f"type {ot} -> {nt}")
            return
        if ot == "string" and o.get("format") != n.get("format"):
            self._add(WARNING, "format-changed", w, f"format {o.get('format')} -> {n.get('format')}")
        self._enum(w, o, n, d)
        self._limits(w, o, n, d)
        if ot == "object" or "properties" in o or "properties" in n:
            self._object(w, o, n, d, seen)
        if ot == "array" or "items" in o or "items" in n:
            self._schema(w + "[]", o.get("items", {}), n.get("items", {}), d, seen)

    def _enum(self, w, o, n, d):
        oe, ne = o.get("enum"), n.get("enum")
        if oe is None and ne is None:
            return
        if oe is None:  # constraint introduced
            if d == REQUEST:
                self._add(BREAKING, "enum-added", w, "enum constraint introduced on request")
            return
        if ne is None:
            if d == RESPONSE:
                self._add(BREAKING, "enum-removed", w, "enum constraint dropped on response (open value set)")
            return
        removed, added = [v for v in oe if v not in ne], [v for v in ne if v not in oe]
        if d == REQUEST and removed:
            self._add(BREAKING, "enum-value-removed", w, f"request enum values removed: {removed}")
        if d == RESPONSE and added:
            self._add(BREAKING, "enum-value-added", w, f"response enum values added: {added}")
        if d == REQUEST and added:
            self._add(SAFE, "enum-value-added", w, f"request enum values added: {added}")
        if d == RESPONSE and removed:
            self._add(SAFE, "enum-value-removed", w, f"response enum values removed: {removed}")

    def _limits(self, w, o, n, d):
        for kw, tighter in _LIMITS.items():
            ov, nv = o.get(kw), n.get(kw)
            if ov == nv:
                continue
            if ov is None:
                tightened = True
            elif nv is None:
                tightened = False
            else:
                tightened = (nv - ov) * tighter > 0
            # request tightening breaks senders; response loosening breaks readers
            if (d == REQUEST and tightened) or (d == RESPONSE and not tightened):
                self._add(BREAKING, "constraint-changed", w, f"{kw} {ov} -> {nv}")
            else:
                self._add(SAFE, "constraint-relaxed" if d == REQUEST else "constraint-tightened", w, f"{kw} {ov} -> {nv}")

    def _object(self, w, o, n, d, seen):
        op, np = o.get("properties", {}) or {}, n.get("properties", {}) or {}
        oreq, nreq = set(o.get("required", [])), set(n.get("required", []))
        for name in sorted(op.keys() - np.keys()):
            pw = f"{w}.{name}"
            if d == RESPONSE:
                self._add(BREAKING, "property-removed", pw, "response property removed")
            else:
                self._add(WARNING, "property-removed", pw, "request property removed (now ignored or rejected)")
        for name in sorted(np.keys() - op.keys()):
            pw = f"{w}.{name}"
            if d == REQUEST and name in nreq:
                self._add(BREAKING, "property-added-required", pw, "new required request property")
            else:
                self._add(SAFE, "property-added", pw, "property added")
        for name in sorted(op.keys() & np.keys()):
            pw = f"{w}.{name}"
            if d == REQUEST and name in nreq and name not in oreq:
                self._add(BREAKING, "property-now-required", pw, "request property became required")
            if d == RESPONSE and name in oreq and name not in nreq:
                self._add(BREAKING, "property-now-optional", pw, "response property no longer guaranteed")
            self._schema(pw, op[name], np[name], d, seen)


def diff_specs(old: dict, new: dict) -> list[Change]:
    return Differ(old, new).diff()
