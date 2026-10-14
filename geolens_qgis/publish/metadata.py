"""Map QGIS layer metadata onto the fields of a manifest dataset entry."""

from __future__ import annotations

from typing import Any

MAX_TAG_LENGTH = 100
MAX_ATTRIBUTES = 500
MAX_ATTRIBUTE_TITLE = 500
MAX_ATTRIBUTE_DESCRIPTION = 5000


def _clean(value: Any) -> str:
    return (value or "").strip()


def _tags(keywords: dict[str, list[str]]) -> list[str]:
    seen: dict[str, None] = {}
    for words in keywords.values():
        for word in words:
            tag = _clean(word)[:MAX_TAG_LENGTH].strip()
            if tag:
                seen.setdefault(tag)
    return list(seen)


def _texts(values) -> list[str]:
    return [text for text in (_clean(value) for value in values) if text]


def _has_content(metadata) -> bool:
    return bool(
        _clean(metadata.title())
        or _clean(metadata.abstract())
        or _tags(metadata.keywords())
        or _texts(metadata.licenses())
        or _texts(metadata.rights())
    )


def _attributes(layer) -> list[dict[str, str]]:
    if not hasattr(layer, "fields"):
        return []
    entries = []
    for field in layer.fields():
        title = _clean(field.alias())
        description = _clean(field.comment())
        if not (title or description):
            continue
        entry = {"source_name": field.name()}
        if title:
            entry["title"] = title[:MAX_ATTRIBUTE_TITLE]
        if description:
            entry["description"] = description[:MAX_ATTRIBUTE_DESCRIPTION]
        entries.append(entry)
    return entries[:MAX_ATTRIBUTES]


def dataset_metadata(layer, project_metadata) -> dict[str, Any]:
    """Return the manifest fields a layer's metadata fills in.

    A layer with no metadata of its own takes the project's abstract and
    keywords. The title comes from the layer alone, because a project title
    names the map, not each dataset in it. Empty fields are left out.
    """
    source = layer.metadata()
    if not _has_content(source):
        source = project_metadata
    entry: dict[str, Any] = {
        "title": _clean(layer.metadata().title()) or layer.name(),
    }
    description = _clean(source.abstract())
    if description:
        entry["description"] = description
    extra: dict[str, Any] = {}
    tags = _tags(source.keywords())
    if tags:
        extra["tags"] = tags
    # Project metadata has no licences or rights.
    licenses = _texts(source.licenses()) if hasattr(source, "licenses") else []
    if licenses:
        extra["license"] = licenses[0]
    rights = _texts(source.rights()) if hasattr(source, "rights") else []
    if rights:
        extra["attribution"] = "; ".join(rights)
    if extra:
        entry["metadata"] = extra
    attributes = _attributes(layer)
    if attributes:
        entry["attributes"] = attributes
    return entry
