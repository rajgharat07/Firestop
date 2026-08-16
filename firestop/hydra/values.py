"""Decode Hydra HTTP value tags (row `{type,value}` and path `{Integer:...}`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_ROW_SCALAR_TAGS = frozenset(
    {"vertex_id", "integer", "signed_integer", "float", "boolean", "string"}
)

# Externally tagged property variants, as they appear inside a path.
_PROPERTY_SCALAR_TAGS = frozenset(
    {"String", "Integer", "SignedInteger", "Float", "Bool", "Boolean", "VertexId"}
)


@dataclass(frozen=True, slots=True)
class GraphNode:
    """A vertex as returned inside a path, with its labels and properties."""

    id: int
    labels: tuple[str, ...] = ()
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.labels[0] if self.labels else ""

    def get(self, name: str, default: Any = None) -> Any:
        return self.properties.get(name, default)


@dataclass(frozen=True, slots=True)
class PathStep:
    """One hop of a path."""

    relationship_type: str
    start: int
    end: int
    properties: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.properties.get(name, default)


@dataclass(frozen=True, slots=True)
class GraphPath:
    """A whole path, as yielded by `algo.SPpaths`, `algo.SSpaths`, `algo.MSpaths`."""

    nodes: list[GraphNode] = field(default_factory=list)
    steps: list[PathStep] = field(default_factory=list)

    @property
    def vertices(self) -> list[int]:
        return [node.id for node in self.nodes]

    @property
    def source(self) -> GraphNode | None:
        return self.nodes[0] if self.nodes else None

    @property
    def target(self) -> GraphNode | None:
        return self.nodes[-1] if self.nodes else None

    @property
    def length(self) -> int:
        return len(self.steps)


def decode_value(value: Any) -> Any:
    """Decode one internally tagged row value."""
    if not isinstance(value, dict):
        return value
    if "type" not in value:
        return decode_property(value)

    tag = value["type"]
    if tag == "null":
        return None
    if tag in _ROW_SCALAR_TAGS:
        return value.get("value")
    if tag == "list":
        return [decode_value(item) for item in value.get("value") or []]
    if tag == "path":
        return decode_path(value.get("value") or {})
    # An unrecognised tag keeps its payload rather than being dropped, so a
    # HydraDB upgrade that adds a variant degrades to opaque data, not silence.
    return value.get("value", value)


def decode_property(value: Any) -> Any:
    """Decode one externally tagged property value from inside a path."""
    if not isinstance(value, dict):
        return value
    if "type" in value:
        return decode_value(value)
    if len(value) != 1:
        return value

    tag, payload = next(iter(value.items()))
    if tag in _PROPERTY_SCALAR_TAGS:
        return payload
    if tag == "Null":
        return None
    if tag == "List":
        return [decode_property(item) for item in payload or []]
    return payload


def decode_path(raw: dict[str, Any]) -> GraphPath:
    """Build a GraphPath from HydraDB's path representation."""
    nodes = [_node(entry) for entry in raw.get("nodes") or []]
    steps = [_step(entry) for entry in raw.get("relationships") or []]

    # Some shapes carry only the hops. Rebuilding endpoints from them keeps
    # source and target meaningful either way.
    if not nodes and steps:
        ids = [steps[0].start, *(step.end for step in steps)]
        nodes = [GraphNode(id=vertex) for vertex in ids]

    return GraphPath(nodes=nodes, steps=steps)


def _node(entry: Any) -> GraphNode:
    if not isinstance(entry, dict):
        return GraphNode(id=_as_int(entry))
    return GraphNode(
        id=_as_int(entry.get("id")),
        labels=tuple(entry.get("labels") or ()),
        properties=_properties(entry.get("properties")),
    )


def _step(entry: Any) -> PathStep:
    if not isinstance(entry, dict):
        return PathStep(relationship_type="", start=0, end=0)
    return PathStep(
        relationship_type=str(entry.get("edge_type") or ""),
        start=_as_int(entry.get("src")),
        end=_as_int(entry.get("dst")),
        properties=_properties(entry.get("properties")),
    )


def _properties(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {name: decode_property(value) for name, value in raw.items()}


def _as_int(value: Any) -> int:
    if isinstance(value, dict):
        value = decode_property(value)
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
