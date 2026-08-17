"""Split name@version (and pnpm/yarn peer suffixes) into name + version."""

from __future__ import annotations


def split(coordinate: str) -> tuple[str, str]:
    """Return `(name, version)`, or `("", "")` if the key is not a coordinate."""
    text = coordinate.strip().strip("'\"")
    if not text:
        return "", ""

    # `react-dom@18.2.0(react@18.2.0)` -- the parenthetical records which peer the
    # entry was resolved against, and is not part of the version.
    depth = 0
    for index, character in enumerate(text):
        if character == "(":
            if depth == 0:
                text = text[:index]
                break
            depth += 1

    text = text.rstrip()
    if text.startswith("/"):
        # pnpm wrote a leading slash until v9.
        text = text[1:]

    scoped = text.startswith("@")
    separator = text.rfind("@", 1 if scoped else 0)

    if separator > 0:
        name, version = text[:separator], text[separator + 1 :]
        if name and version:
            return name, _clean(version)

    # pnpm v5 and earlier separated with a slash: `/lodash/4.17.21`.
    separator = text.rfind("/")
    if separator > 0:
        name, version = text[:separator], text[separator + 1 :]
        if name and version and version[0].isdigit():
            return name, _clean(version)

    return "", ""


def from_module_path(path: str) -> str:
    """The package name inside an npm `node_modules/...` key.

    Nested installs stack: `node_modules/a/node_modules/@scope/b` is `@scope/b`,
    and only the last segment after the final `node_modules/` is the package.
    """
    marker = "node_modules/"
    index = path.rfind(marker)
    if index < 0:
        return ""
    return path[index + len(marker) :].strip("/")


def _clean(version: str) -> str:
    # yarn berry writes `npm:4.17.21` for aliased and plain entries alike.
    if version.startswith("npm:"):
        version = version[4:]
        _, _, tail = version.rpartition("@")
        if tail:
            version = tail
    return version.strip()
