import copy
import json
from pathlib import Path

from apidiff.cli import main
from apidiff.differ import diff_specs

ROOT = Path(__file__).resolve().parent.parent


def base():
    return {"openapi": "3.0.3", "paths": {"/a": {"post": {
        "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}],
        "requestBody": {"content": {"application/json": {"schema": {
            "type": "object", "required": ["x"],
            "properties": {"x": {"type": "string", "enum": ["a", "b"]}, "y": {"type": "integer"}}}}}},
        "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
            "type": "object", "required": ["id"],
            "properties": {"id": {"type": "string"}, "k": {"type": "string", "enum": ["u", "v"]}}}}}}}}}}}


def codes(old, new, sev=None):
    return {(c.code) for c in diff_specs(old, new) if sev is None or c.severity == sev}


def mut(fn):
    d = copy.deepcopy(base())
    fn(d)
    return d


REQ = lambda d: d["paths"]["/a"]["post"]["requestBody"]["content"]["application/json"]["schema"]
RES = lambda d: d["paths"]["/a"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]


def test_identical_has_no_changes():
    assert diff_specs(base(), base()) == []


def test_operation_and_path_removed():
    new = mut(lambda d: d["paths"].pop("/a"))
    assert codes(base(), new, "breaking") == {"path-removed"}
    new = mut(lambda d: d["paths"]["/a"].update({"get": {"responses": {}}}) or d["paths"]["/a"].pop("post"))
    assert "operation-removed" in codes(base(), new, "breaking")


def test_new_required_param_breaking_optional_safe():
    p = lambda req: (lambda d: d["paths"]["/a"]["post"]["parameters"].append(
        {"name": "z", "in": "header", "required": req, "schema": {"type": "string"}}))
    assert "param-added-required" in codes(base(), mut(p(True)), "breaking")
    assert codes(base(), mut(p(False))) == {"param-added"}


def test_request_enum_removal_breaks_but_addition_safe():
    REQ_ENUM = lambda v: (lambda d: REQ(d)["properties"]["x"].update(enum=v))
    assert "enum-value-removed" in codes(base(), mut(REQ_ENUM(["a"])), "breaking")
    assert codes(base(), mut(REQ_ENUM(["a", "b", "c"])), "breaking") == set()


def test_response_enum_direction_is_inverted():
    f = lambda v: (lambda d: RES(d)["properties"]["k"].update(enum=v))
    assert "enum-value-added" in codes(base(), mut(f(["u", "v", "w"])), "breaking")
    assert codes(base(), mut(f(["u"])), "breaking") == set()


def test_property_removal_direction():
    assert "property-removed" in codes(base(), mut(lambda d: RES(d)["properties"].pop("k")), "breaking")
    assert codes(base(), mut(lambda d: REQ(d)["properties"].pop("y")), "breaking") == set()
    assert "property-removed" in codes(base(), mut(lambda d: REQ(d)["properties"].pop("y")), "warning")


def test_required_changes():
    assert "property-now-required" in codes(base(), mut(lambda d: REQ(d).update(required=["x", "y"])), "breaking")
    assert "property-now-optional" in codes(base(), mut(lambda d: RES(d).update(required=[])), "breaking")
    assert codes(base(), mut(lambda d: REQ(d).update(required=[])), "breaking") == set()


def test_new_required_request_property():
    def f(d):
        REQ(d)["properties"]["n"] = {"type": "string"}
        REQ(d)["required"].append("n")
    assert "property-added-required" in codes(base(), mut(f), "breaking")


def test_type_change_and_integer_widening():
    assert "type-changed" in codes(base(), mut(lambda d: REQ(d)["properties"]["y"].update(type="string")), "breaking")
    assert codes(base(), mut(lambda d: REQ(d)["properties"]["y"].update(type="number")), "breaking") == set()
    old = mut(lambda d: RES(d)["properties"]["id"].update(type="integer"))
    new = mut(lambda d: RES(d)["properties"]["id"].update(type="number"))
    assert "type-changed" in codes(old, new, "breaking")


def test_numeric_limits_direction():
    old = mut(lambda d: REQ(d)["properties"]["y"].update(maximum=10))
    tight = mut(lambda d: REQ(d)["properties"]["y"].update(maximum=5))
    loose = mut(lambda d: REQ(d)["properties"]["y"].update(maximum=20))
    assert "constraint-changed" in codes(old, tight, "breaking")
    assert codes(old, loose, "breaking") == set()
    # response: loosening is the breaking direction
    rold = mut(lambda d: RES(d)["properties"]["id"].update(maxLength=10))
    rloose = mut(lambda d: RES(d)["properties"]["id"].update(maxLength=20))
    assert "constraint-changed" in codes(rold, rloose, "breaking")


def test_ref_resolution_and_recursive_schema_terminates():
    def spec(extra):
        return {"paths": {"/t": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Node"}}}}}}}},
            "components": {"schemas": {"Node": {"type": "object", "properties": {
                "child": {"$ref": "#/components/schemas/Node"}, "v": {"type": extra}}}}}}
    assert codes(spec("string"), spec("string")) == set()
    assert "type-changed" in codes(spec("string"), spec("integer"), "breaking")


def test_response_status_removed_added():
    assert "response-removed" in codes(base(), mut(lambda d: d["paths"]["/a"]["post"]["responses"].pop("200")), "breaking")
    new = mut(lambda d: d["paths"]["/a"]["post"]["responses"].update({"500": {"description": "x"}}))
    assert "response-added" in codes(base(), new, "warning")


def test_request_body_became_required():
    old = mut(lambda d: d["paths"]["/a"]["post"]["requestBody"].update(required=False))
    new = mut(lambda d: d["paths"]["/a"]["post"]["requestBody"].update(required=True))
    assert "body-now-required" in codes(old, new, "breaking")


def test_path_level_params_overridden_by_operation():
    old = mut(lambda d: d["paths"]["/a"].update(parameters=[{"name": "q", "in": "query", "required": True, "schema": {"type": "string"}}]))
    # operation-level 'q' (optional) overrides the path-level required one
    assert codes(old, old) == set()


def test_sample_matches_expected_output(capsys):
    rc = main([str(ROOT / "samples/petstore_v1.json"), str(ROOT / "samples/petstore_v2.json")])
    assert rc == 1
    assert capsys.readouterr().out == (ROOT / "samples/expected_output.txt").read_text()


def test_cli_exit_codes_and_json(tmp_path, capsys):
    v1 = str(ROOT / "samples/petstore_v1.json")
    assert main([v1, v1]) == 0
    assert main([v1, str(ROOT / "samples/petstore_v2.json"), "--fail-on", "never"]) == 0
    capsys.readouterr()
    main([v1, str(ROOT / "samples/petstore_v2.json"), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["severity"] == "breaking" and {"code", "where", "message"} <= data[0].keys()
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert main([v1, str(bad)]) == 2
