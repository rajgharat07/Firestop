from __future__ import annotations

from firestop.hydra.values import (
    GraphPath,
    decode_path,
    decode_property,
    decode_value,
)

# Captured from a live node, from
# CALL algo.MSpaths(...) YIELD path RETURN path.
LIVE_PATH = {
    "nodes": [
        {
            "id": 1853108085969174433,
            "labels": ["Service"],
            "properties": {
                "criticality": {"Integer": 3},
                "name": {"String": "checkout-api"},
                "repo": {"String": "acme/checkout"},
            },
        },
        {
            "id": 5892610373482916351,
            "labels": ["Lockfile"],
            "properties": {
                "committed_at": {"Integer": 1700000000},
                "path": {"String": "package-lock.json"},
                "service": {"String": "checkout-api"},
            },
        },
        {
            "id": 4738061942266031222,
            "labels": ["Release"],
            "properties": {
                "deprecated": {"Bool": False},
                "package": {"String": "evil"},
                "published_at": {"Integer": 1600000002},
                "version": {"String": "1.4.2"},
            },
        },
    ],
    "relationships": [
        {
            "id": 5,
            "edge_type": "USES_LOCKFILE",
            "src": 1853108085969174433,
            "dst": 5892610373482916351,
            "properties": {"id": {"Integer": 1888821365012162288}},
        },
        {
            "id": 7,
            "edge_type": "PINS",
            "src": 5892610373482916351,
            "dst": 4738061942266031222,
            "properties": {
                "id": {"Integer": 1871645640040664409},
                "resolved_version": {"String": "1.4.2"},
                "valid_from": {"Integer": 1600000000},
                "valid_to": {"Integer": 4102444800},
            },
        },
    ],
}


def test_row_scalars_lose_their_envelope():
    assert decode_value({"type": "string", "value": "lodash"}) == "lodash"
    assert decode_value({"type": "integer", "value": 7}) == 7
    assert decode_value({"type": "signed_integer", "value": -7}) == -7
    assert decode_value({"type": "float", "value": 0.5}) == 0.5
    assert decode_value({"type": "boolean", "value": False}) is False
    assert decode_value({"type": "vertex_id", "value": 12}) == 12
    assert decode_value({"type": "null"}) is None


def test_row_lists_decode_their_members():
    envelope = {
        "type": "list",
        "value": [{"type": "integer", "value": 1}, {"type": "string", "value": "a"}],
    }
    assert decode_value(envelope) == [1, "a"]


def test_unknown_row_tag_is_kept_rather_than_dropped():
    assert decode_value({"type": "future_type", "value": {"a": 1}}) == {"a": 1}


def test_untagged_scalar_passes_through():
    assert decode_value("plain") == "plain"
    assert decode_value(5) == 5


def test_path_properties_use_the_externally_tagged_encoding():
    # Path props use external tags ({Integer: 7}).
    assert decode_property({"String": "lodash"}) == "lodash"
    assert decode_property({"Integer": 7}) == 7
    assert decode_property({"SignedInteger": -7}) == -7
    assert decode_property({"Float": 0.25}) == 0.25
    assert decode_property({"Bool": False}) is False
    assert decode_property({"Null": None}) is None
    assert decode_property({"List": [{"Integer": 1}, {"String": "a"}]}) == [1, "a"]


def test_property_decoder_accepts_a_row_envelope_too():
    assert decode_property({"type": "string", "value": "x"}) == "x"


def test_ambiguous_property_shape_is_returned_intact():
    assert decode_property({"a": 1, "b": 2}) == {"a": 1, "b": 2}


def test_live_path_decodes_nodes_with_labels_and_properties():
    path = decode_path(LIVE_PATH)

    assert isinstance(path, GraphPath)
    assert path.vertices == [
        1853108085969174433,
        5892610373482916351,
        4738061942266031222,
    ]
    assert path.length == 2
    assert [node.label for node in path.nodes] == ["Service", "Lockfile", "Release"]
    assert path.source.get("name") == "checkout-api"
    assert path.target.get("package") == "evil"
    assert path.target.get("version") == "1.4.2"
    assert path.nodes[0].get("criticality") == 3


def test_live_path_decodes_relationship_type_and_endpoints():
    path = decode_path(LIVE_PATH)

    assert [step.relationship_type for step in path.steps] == ["USES_LOCKFILE", "PINS"]
    assert path.steps[0].start == 1853108085969174433
    assert path.steps[0].end == 5892610373482916351
    # Hops chain end to start.
    assert path.steps[0].end == path.steps[1].start


def test_live_path_exposes_edge_properties_for_interval_checks():
    path = decode_path(LIVE_PATH)
    pins = path.steps[1]

    assert pins.get("resolved_version") == "1.4.2"
    assert pins.get("valid_from") == 1600000000
    assert pins.get("valid_to") == 4102444800


def test_path_inside_a_row_envelope():
    decoded = decode_value({"type": "path", "value": LIVE_PATH})

    assert isinstance(decoded, GraphPath)
    assert decoded.length == 2


def test_empty_path_is_safe_to_read():
    path = decode_path({})

    assert path.vertices == []
    assert path.length == 0
    assert path.source is None
    assert path.target is None


def test_path_without_a_node_list_is_rebuilt_from_its_hops():
    path = decode_path(
        {
            "relationships": [
                {"edge_type": "PINS", "src": 10, "dst": 11, "properties": {}},
                {"edge_type": "DEPENDS_ON", "src": 11, "dst": 12, "properties": {}},
            ]
        }
    )

    assert path.vertices == [10, 11, 12]
    assert path.source.id == 10
    assert path.target.id == 12
