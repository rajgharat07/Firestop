from __future__ import annotations

import re

from firestop.schema.model import (
    EXPOSURE_RELS,
    OPEN_INTERVAL_END,
    UPSERT_DEPENDS_ON,
    UPSERT_PACKAGES,
    UPSERT_VERSION_OF,
    Label,
    Rel,
    upsert_edges,
    upsert_vertices,
)


def test_vertex_upsert_merges_on_id_alone():
    # MERGE on id only; other props in SET.
    statement = upsert_vertices(Label.PACKAGE, ("name", "ecosystem"))

    assert "MERGE (n {id: row.id})" in statement
    assert statement.count("MERGE") == 1
    merge_pattern = re.search(r"MERGE \((.*?)\)", statement).group(1)
    assert "name" not in merge_pattern


def test_vertex_upsert_sets_label_and_properties():
    statement = upsert_vertices(Label.PACKAGE, ("name", "ecosystem"))

    assert "SET n:Package" in statement
    assert "n.name = row.name" in statement
    assert "n.ecosystem = row.ecosystem" in statement


def test_vertex_upsert_is_driven_by_a_parameter():
    # Batch via parameter, not inline list.
    assert UPSERT_PACKAGES.startswith("UNWIND $rows AS row")


def test_edge_upsert_matches_both_endpoints_before_merging():
    statement = upsert_edges(Rel.PINS, Label.LOCKFILE, Label.RELEASE, ("resolved_version",))

    assert "MATCH (s:Lockfile {id: row.source}), (d:Release {id: row.target})" in statement
    assert "MERGE (s)-[r:PINS {id: row.id}]->(d)" in statement
    assert "SET r.resolved_version = row.resolved_version" in statement


def test_edge_upsert_without_properties_omits_set():
    assert "SET" not in UPSERT_VERSION_OF


def test_edge_upsert_is_directed_and_single_typed():
    # One rel type per pattern; directed only.
    for statement in (UPSERT_VERSION_OF, UPSERT_DEPENDS_ON):
        assert "]->" in statement
        assert "<-[" not in statement
        assert "|" not in statement


def test_depends_on_carries_its_validity_window():
    assert "r.valid_from = row.valid_from" in UPSERT_DEPENDS_ON
    assert "r.valid_to = row.valid_to" in UPSERT_DEPENDS_ON
    assert "r.resolved_to = row.resolved_to" in UPSERT_DEPENDS_ON


def test_open_interval_end_is_an_ordinary_sortable_integer():
    # Open end is a far-future sentinel, not null.
    assert isinstance(OPEN_INTERVAL_END, int)
    assert OPEN_INTERVAL_END > 2_000_000_000


def test_exposure_path_crosses_the_consumer_and_ecosystem_planes():
    assert EXPOSURE_RELS == (Rel.USES_LOCKFILE, Rel.PINS, Rel.DEPENDS_ON)


def test_exposure_rels_need_a_path_procedure():
    # Multi-type walk needs algo.MSpaths.
    assert len(set(EXPOSURE_RELS)) > 1
