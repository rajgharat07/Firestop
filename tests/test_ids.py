from __future__ import annotations

from firestop.ids import (
    Kind,
    edge_id,
    package_id,
    release_id,
    service_id,
    vertex_id,
)

MAX_VERTEX_ID = (1 << 63) - 1


def test_ids_are_stable_across_calls():
    assert package_id("lodash") == package_id("lodash")
    assert release_id("lodash", "4.17.21") == release_id("lodash", "4.17.21")


def test_ids_fit_a_positive_signed_64_bit_integer():
    # Ids stay non-negative for Bolt signed ints.
    for key in ("lodash", "react", "@babel/core", "a" * 512, "", "ünicode"):
        assert 0 <= package_id(key) <= MAX_VERTEX_ID


def test_kinds_do_not_collide_on_the_same_key():
    key = "checkout-api"
    ids = {vertex_id(kind, key) for kind in Kind}
    assert len(ids) == len(Kind)


def test_distinct_coordinates_get_distinct_ids():
    assert release_id("lodash", "4.17.20") != release_id("lodash", "4.17.21")
    assert package_id("lodash") != package_id("lodash-es")
    assert package_id("react", "npm") != package_id("react", "pypi")


def test_release_and_package_ids_are_independent():
    assert package_id("lodash") != release_id("lodash", "4.17.21")


def test_service_ids_are_not_package_ids():
    assert service_id("lodash") != package_id("lodash")


def test_edge_ids_depend_on_type_and_both_endpoints():
    a, b = package_id("a"), package_id("b")

    assert edge_id("DEPENDS_ON", a, b) == edge_id("DEPENDS_ON", a, b)
    assert edge_id("DEPENDS_ON", a, b) != edge_id("DEPENDS_ON", b, a)
    assert edge_id("DEPENDS_ON", a, b) != edge_id("PINS", a, b)
    assert 0 <= edge_id("DEPENDS_ON", a, b) <= MAX_VERTEX_ID


def test_ids_are_stable_across_processes():
    # Must be stable across process restarts (not Python hash()).
    assert package_id("lodash") == 8824599796979468112
    assert release_id("lodash", "4.17.21") == 4088440188503746918
