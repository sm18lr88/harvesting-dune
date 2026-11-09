"""docs/sync-openapi.yaml against the guild server: routes, field allowlists, bounds and error shapes."""

import json
from pathlib import Path

import pytest
from guildsync import api, datasets, fields, httpd, presence, problems, profiles, profilestore

ROOT = Path(__file__).parent.parent
SPEC = json.loads((ROOT / "docs" / "sync-openapi.yaml").read_text(encoding="utf-8"))  # JSON syntax is valid YAML
SCHEMAS = SPEC["components"]["schemas"]
CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "sync-contract.json").read_text(encoding="utf-8"))
RATES = {"key": httpd.KEY_RATE, "ip": httpd.IP_RATE}


def test_the_spec_lists_exactly_the_routes_the_server_answers():
    served = {(httpd.HEALTH, m.strip().lower()) for m in httpd.HEALTH_METHODS.split(",")}
    served |= {(path, "post") for path in httpd.ROUTES}
    served |= {(path, m.strip().lower()) for path in (httpd.DATA, httpd.DATA + "/{name}") for m in httpd.DATA_METHODS.split(",")}
    assert {(path, m) for path, item in SPEC["paths"].items() for m in item} == served
    assert {path: item["post"]["operationId"] for path, item in SPEC["paths"].items() if "post" in item} == httpd.ROUTES


@pytest.mark.parametrize(("schema", "allowed"), [
    ("PresenceBeat", api.PRESENCE_FIELDS), ("Record", api.RECORD_FIELDS), ("Identity", fields.IDENTITY_FIELDS),
    ("Partition", api.PARTITION_FIELDS), ("Position", api.POINT_FIELDS), ("ProfileUpload", profiles.PROFILE_BODY),
    ("Profile", profiles.PROFILE_FIELDS), ("Session", profiles.SESSION_FIELDS), ("ProfilesRequest", profiles.PROFILES_BODY),
])
def test_request_schemas_are_closed_and_match_the_server_allowlists(schema, allowed):
    s = SCHEMAS[schema]
    assert list(s["properties"]) == list(allowed) and s["required"] == list(allowed)
    assert s["additionalProperties"] is False


def test_the_sync_request_lists_its_optional_fields_apart():
    s = SCHEMAS["SyncRequest"]
    assert list(s["properties"]) == [*api.BODY_FIELDS, *api.OPTIONAL_BODY_FIELDS] and s["required"] == list(api.BODY_FIELDS)
    assert s["additionalProperties"] is False


def test_the_spec_declares_the_presence_states_and_bounds():
    assert SCHEMAS["PresenceBeat"]["properties"]["nextS"]["x-clamp"] == list(presence.NEXT_S_RANGE)
    assert SCHEMAS["Member"]["properties"]["state"]["enum"] == list(presence.STATES)
    assert SCHEMAS["StoredProfile"]["properties"]["playerTag"]["maxLength"] == profilestore.TAG_HEX


def test_the_spec_declares_the_server_bounds():
    record, plain = SCHEMAS["Record"]["properties"], SCHEMAS["PlainValue"]
    assert SCHEMAS["SyncRequest"]["properties"]["records"]["maxItems"] == api.MAX_RECORDS
    assert set(record["cls"]["enum"]) == api.CLASSES and set(record["size"]["enum"]) == api.SIZES | {None}
    assert (plain["maxLength"], plain["maxItems"], plain["maxProperties"], plain["x-max-depth"]) == (
        profiles.MAX_TEXT, profiles.MAX_ITEMS, profiles.MAX_KEYS, profiles.MAX_DEPTH)
    assert SCHEMAS["Profile"]["x-max-stored-bytes"] == profiles.MAX_PROFILE
    for path, item in SPEC["paths"].items():
        for op in item.values():
            limit = op["x-rate-limit"]
            assert (limit["bucket"], limit["bucket"] / limit["perSeconds"]) == RATES[limit["by"]], path
            if "requestBody" in op:
                assert op["requestBody"]["x-max-body-bytes"] == httpd.MAX_BODY


def test_the_spec_declares_the_dataset_rules():
    one, listing = SPEC["paths"][httpd.DATA + "/{name}"], SPEC["paths"][httpd.DATA]
    for op in one.values():
        [name] = [p for p in op["parameters"] if p.get("in") == "path"]
        assert (name["schema"]["pattern"], name["schema"]["maxLength"]) == (f"^{datasets.NAME.pattern}$", 32)
        assert op["x-max-dataset-bytes"] == datasets.MAX_BYTES and {"200", "304", "401", "404"} <= set(op["responses"])
    assert set(listing) == set(one) == {"get", "head"}
    assert SCHEMAS["DatasetList"]["properties"]["datasets"]["maxItems"] == datasets.MAX_LISTED
    assert SCHEMAS["DatasetInfo"]["properties"]["bytes"]["maximum"] == datasets.MAX_BYTES


def test_every_problem_type_the_server_mints_is_declared():
    declared = set(SCHEMAS["Problem"]["x-problem-types"])
    assert declared == {"about:blank"} | {problems.BASE + kind for kind in problems.TYPES}


@pytest.mark.parametrize("case", [c for c in CONTRACT["cases"] if c["expectedStatus"] >= 400], ids=lambda c: c["name"])
def test_every_refusal_in_the_contract_is_a_declared_problem_response(case):
    """test_sync_server proves the server answers these cases exactly; here the spec must declare them."""
    req, body = case["request"], case["expectedResponse"]
    op = SPEC["paths"].get(req["path"], {}).get(req["method"].lower())
    status = str(case["expectedStatus"])
    if op is not None:  # an unknown path (404) or method (405) has no operation to declare it
        assert status in op["responses"], (case["name"], status)
        ref = op["responses"][status]["$ref"].rsplit("/", 1)[-1]
        assert "application/problem+json" in SPEC["components"]["responses"][ref]["content"]
    assert set(SCHEMAS["Problem"]["required"]) <= set(body) and body["status"] == case["expectedStatus"]
    assert body["type"] in SCHEMAS["Problem"]["x-problem-types"]
