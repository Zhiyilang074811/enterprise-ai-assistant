"""租户知识资产标签与智能体知识范围。"""
from __future__ import annotations

import hashlib
import json

from backend.document_processing import normalize_tier
from backend.tenant_config import ensure_tenant_storage, get_tenant_knowledge_metadata_path

PUBLIC_TIER_CODE_MAP = {
    "permanent": "L1",
    "seasonal": "L2",
    "incremental": "L2",
    "hotfix": "L3",
}

FILE_META_EXTRA_KEYS = {
    "source_type",
    "original_suffix",
    "display_mode",
    "parser_chain",
    "parse_mode",
    "pdf_mode",
    "table_header_mode",
    "preview",
    "source_name",
}


def _public_tier_code(value: str) -> str:
    canonical = normalize_tier(value)
    return PUBLIC_TIER_CODE_MAP.get(canonical, canonical)


def _metadata_key(tier: str, file_name: str) -> str:
    canonical = normalize_tier(tier)
    clean_file = str(file_name or "").strip().replace("\\", "/").lstrip("/")
    return f"{canonical}/{clean_file}"


def _find_metadata_key(items: dict[str, dict], tier: str, file_name: str) -> str:
    direct_key = _metadata_key(tier, file_name)
    if direct_key in items:
        return direct_key
    clean_file = str(file_name or "").strip().replace("\\", "/").lstrip("/")
    for key, value in items.items():
        if not isinstance(value, dict):
            continue
        existing_name = str(value.get("file") or key.split("/", 1)[-1] or "").strip().replace("\\", "/").lstrip("/")
        if existing_name == clean_file:
            return key
    return direct_key


def _stable_id(prefix: str, seed: str) -> str:
    clean = str(seed or "").strip()
    if not clean:
        clean = prefix
    return f"{prefix}_{hashlib.md5(clean.encode('utf-8')).hexdigest()[:10]}"


def _normalize_tags(tags: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in list(tags or []):
        clean = str(raw or "").strip()
        if not clean:
            continue
        lowered = clean.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(clean)
    return result


def _normalize_tag_groups(
    entries: list | None,
    libraries: list[dict] | None = None,
    *,
    default_library_id: str = "",
) -> list[dict]:
    groups: list[dict] = []
    seen_group_ids: set[str] = set()
    normalized_libraries = _normalize_library_records(libraries)
    valid_library_ids = {item["library_id"] for item in normalized_libraries}
    fallback_library_id = default_library_id or normalized_libraries[0]["library_id"]
    for index, raw in enumerate(entries or [], start=1):
        if isinstance(raw, str):
            group_name = str(raw).strip()
            if not group_name:
                continue
            group_id = _stable_id("tag", f"{group_name}:{index}")
            groups.append(
                {
                    "tag_id": group_id,
                    "name": group_name,
                    "library_id": fallback_library_id,
                    "values": [
                        {
                            "value_id": _stable_id("tagv", f"{group_name}:{group_name}"),
                            "name": group_name,
                            "synonyms": [],
                        }
                    ],
                }
            )
            continue
        if not isinstance(raw, dict):
            continue
        group_name = str(raw.get("name") or raw.get("tag_name") or "").strip()
        if not group_name:
            continue
        group_id = str(raw.get("tag_id") or raw.get("id") or "").strip() or _stable_id("tag", f"{group_name}:{index}")
        if group_id in seen_group_ids:
            continue
        seen_group_ids.add(group_id)
        library_id = str(raw.get("library_id") or "").strip() or fallback_library_id
        if library_id not in valid_library_ids:
            library_id = fallback_library_id
        values: list[dict] = []
        seen_value_ids: set[str] = set()
        raw_values = raw.get("values") if isinstance(raw.get("values"), list) else raw.get("tag_values")
        for value_index, value_raw in enumerate(raw_values or [], start=1):
            if isinstance(value_raw, str):
                value_name = str(value_raw).strip()
                synonyms = []
                value_id = _stable_id("tagv", f"{group_id}:{value_name}:{value_index}")
            elif isinstance(value_raw, dict):
                value_name = str(value_raw.get("name") or value_raw.get("value") or "").strip()
                synonyms = _normalize_tags(value_raw.get("synonyms") if isinstance(value_raw.get("synonyms"), list) else [])
                value_id = str(value_raw.get("value_id") or value_raw.get("id") or "").strip() or _stable_id("tagv", f"{group_id}:{value_name}:{value_index}")
            else:
                continue
            if not value_name or value_id in seen_value_ids:
                continue
            seen_value_ids.add(value_id)
            values.append(
                {
                    "value_id": value_id,
                    "name": value_name,
                    "synonyms": synonyms,
                }
            )
        groups.append(
            {
                "tag_id": group_id,
                "name": group_name,
                "library_id": library_id,
                "values": values,
            }
        )
    return groups


def _flatten_tag_group_values(groups: list[dict] | None) -> list[str]:
    values: list[str] = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("values") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name:
                values.append(name)
    return _normalize_tags(values)


def _normalize_library_records(entries: list[dict] | None) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries or [], start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        library_id = str(raw.get("library_id") or raw.get("id") or "").strip() or _stable_id("kb", f"{name}:{index}")
        if library_id in seen:
            continue
        seen.add(library_id)
        result.append(
            {
                "library_id": library_id,
                "name": name,
                "description": str(raw.get("description") or "").strip(),
            }
        )
    if not result:
        result.append({"library_id": "kb_default", "name": "默认知识库", "description": "租户默认知识库"})
    return result


def _normalize_category_records(entries: list[dict] | None, libraries: list[dict]) -> list[dict]:
    valid_library_ids = {item["library_id"] for item in libraries}
    fallback_library_id = libraries[0]["library_id"]
    result: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries or [], start=1):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        library_id = str(raw.get("library_id") or "").strip()
        if library_id not in valid_library_ids:
            library_id = fallback_library_id
        category_id = str(raw.get("category_id") or raw.get("id") or "").strip() or _stable_id("cat", f"{library_id}:{name}:{index}")
        if category_id in seen:
            continue
        seen.add(category_id)
        result.append(
            {
                "category_id": category_id,
                "library_id": library_id,
                "name": name,
            }
        )
    return result


def _extract_file_meta_extras(raw: dict | None) -> dict:
    if not isinstance(raw, dict):
        return {}
    extras: dict = {}
    for key in FILE_META_EXTRA_KEYS:
        value = raw.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "parser_chain" and not isinstance(value, list):
            continue
        if key == "preview" and not isinstance(value, dict):
            continue
        extras[key] = value
    return extras


def load_knowledge_metadata(tenant_id: str, tenant_name: str = "") -> dict:
    ensure_tenant_storage(tenant_id, tenant_name or tenant_id)
    path = get_tenant_knowledge_metadata_path(tenant_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    catalog = data.get("catalog") if isinstance(data, dict) else []
    libraries = data.get("libraries") if isinstance(data, dict) else []
    categories = data.get("categories") if isinstance(data, dict) else []
    items = data.get("items") if isinstance(data, dict) else {}
    normalized_libraries = _normalize_library_records(libraries if isinstance(libraries, list) else [])
    normalized_catalog = _normalize_tag_groups(
        catalog if isinstance(catalog, list) else [],
        normalized_libraries,
    )
    normalized_categories = _normalize_category_records(categories if isinstance(categories, list) else [], normalized_libraries)
    if not isinstance(items, dict):
        items = {}
    normalized: dict[str, dict] = {}
    for key, value in items.items():
        if not isinstance(value, dict):
            continue
        tier = normalize_tier(str(value.get("tier") or key.split("/", 1)[0] or "permanent"))
        file_name = str(value.get("file") or key.split("/", 1)[-1] or "").strip()
        if not file_name:
            continue
        clean_key = _metadata_key(tier, file_name)
        library_id = str(value.get("library_id") or "").strip() or normalized_libraries[0]["library_id"]
        if library_id not in {item["library_id"] for item in normalized_libraries}:
            library_id = normalized_libraries[0]["library_id"]
        category_id = str(value.get("category_id") or "").strip()
        if category_id and category_id not in {item["category_id"] for item in normalized_categories}:
            category_id = ""
        normalized[clean_key] = {
            "tier": tier,
            "tier_code": _public_tier_code(tier),
            "file": file_name,
            "tags": _normalize_tags(value.get("tags") if isinstance(value.get("tags"), list) else []),
            "library_id": library_id,
            "category_id": category_id,
            **_extract_file_meta_extras(value),
        }
    return {
        "catalog": normalized_catalog,
        "libraries": normalized_libraries,
        "categories": normalized_categories,
        "items": normalized,
    }


def save_knowledge_metadata(tenant_id: str, metadata: dict, tenant_name: str = "") -> dict:
    ensure_tenant_storage(tenant_id, tenant_name or tenant_id)
    normalized = load_knowledge_metadata(tenant_id, tenant_name)
    libraries = metadata.get("libraries") if isinstance(metadata, dict) else []
    if isinstance(libraries, list):
        normalized["libraries"] = _normalize_library_records(libraries)
    categories = metadata.get("categories") if isinstance(metadata, dict) else []
    if isinstance(categories, list):
        normalized["categories"] = _normalize_category_records(categories, normalized["libraries"])
    catalog = metadata.get("catalog") if isinstance(metadata, dict) else []
    if isinstance(catalog, list):
        normalized["catalog"] = _normalize_tag_groups(catalog, normalized["libraries"])
    items = metadata.get("items") if isinstance(metadata, dict) else {}
    if isinstance(items, dict):
        normalized["items"] = {}
        for key, value in items.items():
            if not isinstance(value, dict):
                continue
            tier = normalize_tier(str(value.get("tier") or key.split("/", 1)[0] or "permanent"))
            file_name = str(value.get("file") or key.split("/", 1)[-1] or "").strip()
            if not file_name:
                continue
            clean_key = _metadata_key(tier, file_name)
            library_id = str(value.get("library_id") or "").strip() or normalized["libraries"][0]["library_id"]
            if library_id not in {item["library_id"] for item in normalized["libraries"]}:
                library_id = normalized["libraries"][0]["library_id"]
            category_id = str(value.get("category_id") or "").strip()
            if category_id and category_id not in {item["category_id"] for item in normalized["categories"]}:
                category_id = ""
            normalized["items"][clean_key] = {
                "tier": tier,
                "tier_code": _public_tier_code(tier),
                "file": file_name,
                "tags": _normalize_tags(value.get("tags") if isinstance(value.get("tags"), list) else []),
                "library_id": library_id,
                "category_id": category_id,
                **_extract_file_meta_extras(value),
            }
    catalog_groups = [dict(item) for item in (normalized.get("catalog") or []) if isinstance(item, dict)]
    known_values_by_library: dict[str, set[str]] = {}
    for group in catalog_groups:
        library_id = str(group.get("library_id") or normalized["libraries"][0]["library_id"]).strip()
        known_values_by_library.setdefault(library_id, set()).update(_flatten_tag_group_values([group]))
    for item in normalized["items"].values():
        library_id = str(item.get("library_id") or normalized["libraries"][0]["library_id"]).strip()
        known_values = known_values_by_library.setdefault(library_id, set())
        for tag in (item.get("tags") or []):
            clean = str(tag or "").strip()
            if clean and clean not in known_values:
                catalog_groups.append(
                    {
                        "tag_id": _stable_id("tag", f"{library_id}:{clean}"),
                        "name": clean,
                        "library_id": library_id,
                        "values": [
                            {
                                "value_id": _stable_id("tagv", f"{library_id}:{clean}"),
                                "name": clean,
                                "synonyms": [],
                            }
                        ],
                    }
                )
                known_values.add(clean)
    normalized["catalog"] = _normalize_tag_groups(catalog_groups, normalized["libraries"])
    path = get_tenant_knowledge_metadata_path(tenant_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def get_knowledge_file_meta(tenant_id: str, tier: str, file_name: str, tenant_name: str = "") -> dict:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    key = _find_metadata_key(metadata["items"], tier, file_name)
    item = metadata["items"].get(key)
    if item:
        return dict(item)
    canonical = normalize_tier(tier)
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    default_library_id = metadata["libraries"][0]["library_id"]
    return {
        "tier": canonical,
        "tier_code": _public_tier_code(canonical),
        "file": file_name,
        "tags": [],
        "library_id": default_library_id,
        "category_id": "",
    }


def set_knowledge_file_meta(
    tenant_id: str,
    *,
    tier: str,
    file_name: str,
    tags: list[str] | tuple[str, ...] | set[str] | None = None,
    library_id: str = "",
    category_id: str = "",
    asset_meta: dict | None = None,
    tenant_name: str = "",
) -> dict:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    canonical = normalize_tier(tier)
    key = _find_metadata_key(metadata["items"], canonical, file_name)
    existing = metadata["items"].get(key, {})
    library_id = str(library_id or existing.get("library_id") or metadata["libraries"][0]["library_id"]).strip()
    if library_id not in {item["library_id"] for item in metadata["libraries"]}:
        library_id = metadata["libraries"][0]["library_id"]
    category_id = str(category_id or existing.get("category_id") or "").strip()
    valid_categories = {item["category_id"] for item in metadata["categories"] if item["library_id"] == library_id}
    if category_id and category_id not in valid_categories:
        category_id = ""
    metadata["items"][key] = {
        "tier": canonical,
        "tier_code": _public_tier_code(canonical),
        "file": file_name,
        "tags": _normalize_tags(tags if tags is not None else existing.get("tags")),
        "library_id": library_id,
        "category_id": category_id,
        **_extract_file_meta_extras(existing),
        **_extract_file_meta_extras(asset_meta),
    }
    catalog_groups = [dict(item) for item in (metadata.get("catalog") or []) if isinstance(item, dict)]
    known_values = set(
        _flatten_tag_group_values([
            group
            for group in catalog_groups
            if str(group.get("library_id") or metadata["libraries"][0]["library_id"]).strip() == library_id
        ])
    )
    for tag in metadata["items"][key]["tags"]:
        clean = str(tag or "").strip()
        if not clean or clean in known_values:
            continue
        catalog_groups.append(
            {
                "tag_id": _stable_id("tag", f"{library_id}:{clean}"),
                "name": clean,
                "library_id": library_id,
                "values": [
                    {
                        "value_id": _stable_id("tagv", f"{library_id}:{clean}"),
                        "name": clean,
                        "synonyms": [],
                    }
                ],
            }
        )
        known_values.add(clean)
    metadata["catalog"] = _normalize_tag_groups(catalog_groups, metadata["libraries"])
    save_knowledge_metadata(tenant_id, metadata, tenant_name)
    return dict(metadata["items"][key])


def set_knowledge_file_tags(
    tenant_id: str,
    tier: str,
    file_name: str,
    tags: list[str] | tuple[str, ...] | set[str] | None,
    tenant_name: str = "",
) -> dict:
    return set_knowledge_file_meta(
        tenant_id,
        tier=tier,
        file_name=file_name,
        tags=tags,
        tenant_name=tenant_name,
    )


def delete_knowledge_file_meta(tenant_id: str, tier: str, file_name: str, tenant_name: str = "") -> None:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    metadata["items"].pop(_find_metadata_key(metadata["items"], tier, file_name), None)
    save_knowledge_metadata(tenant_id, metadata, tenant_name)


def list_knowledge_tags(tenant_id: str, tenant_name: str = "", library_id: str = "") -> list[str]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    target_library_id = str(library_id or "").strip()
    catalog_groups = [
        item
        for item in (metadata.get("catalog") or [])
        if isinstance(item, dict) and (not target_library_id or str(item.get("library_id") or "").strip() == target_library_id)
    ]
    return _normalize_tags(_flatten_tag_group_values(catalog_groups) + [
        tag
        for item in metadata["items"].values()
        if not target_library_id or str(item.get("library_id") or "").strip() == target_library_id
        for tag in (item.get("tags") or [])
    ])


def list_knowledge_tag_groups(tenant_id: str, tenant_name: str = "", library_id: str = "") -> list[dict]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    target_library_id = str(library_id or "").strip()
    return [
        dict(item)
        for item in (metadata.get("catalog") or [])
        if isinstance(item, dict) and (not target_library_id or str(item.get("library_id") or "").strip() == target_library_id)
    ]


def list_knowledge_libraries(tenant_id: str, tenant_name: str = "") -> list[dict]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    return [dict(item) for item in metadata.get("libraries") or []]


def list_knowledge_categories(tenant_id: str, tenant_name: str = "", library_id: str = "") -> list[dict]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    categories = [dict(item) for item in metadata.get("categories") or []]
    if library_id:
        return [item for item in categories if str(item.get("library_id") or "") == str(library_id)]
    return categories


def save_knowledge_structure(
    tenant_id: str,
    *,
    libraries: list[dict] | None = None,
    categories: list[dict] | None = None,
    tenant_name: str = "",
) -> dict:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    if libraries is not None:
        metadata["libraries"] = _normalize_library_records(libraries)
    if categories is not None:
        metadata["categories"] = _normalize_category_records(categories, metadata["libraries"])
    save_knowledge_metadata(tenant_id, metadata, tenant_name)
    return metadata


def save_knowledge_tag_catalog(tenant_id: str, tags: list[str], tenant_name: str = "", library_id: str = "") -> list[str]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    target_library_id = str(library_id or "").strip()
    next_groups = _normalize_tag_groups(tags, metadata["libraries"], default_library_id=target_library_id)
    if target_library_id:
        metadata["catalog"] = [
            item
            for item in (metadata.get("catalog") or [])
            if isinstance(item, dict) and str(item.get("library_id") or "").strip() != target_library_id
        ] + next_groups
    else:
        metadata["catalog"] = next_groups
    save_knowledge_metadata(tenant_id, metadata, tenant_name)
    return list_knowledge_tags(tenant_id, tenant_name, library_id=target_library_id)


def save_knowledge_tag_groups(tenant_id: str, groups: list[dict], tenant_name: str = "", library_id: str = "") -> list[dict]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    target_library_id = str(library_id or "").strip()
    next_groups = _normalize_tag_groups(groups, metadata["libraries"], default_library_id=target_library_id)
    if target_library_id:
        metadata["catalog"] = [
            item
            for item in (metadata.get("catalog") or [])
            if isinstance(item, dict) and str(item.get("library_id") or "").strip() != target_library_id
        ] + next_groups
    else:
        metadata["catalog"] = next_groups
    save_knowledge_metadata(tenant_id, metadata, tenant_name)
    return list_knowledge_tag_groups(tenant_id, tenant_name, library_id=target_library_id)


def _normalize_knowledge_scope(scope: dict | None) -> dict[str, set[str]]:
    raw = scope if isinstance(scope, dict) else {}
    return {
        "tiers": {
            _public_tier_code(str(item))
            for item in (raw.get("tiers") or [])
            if str(item).strip()
        },
        "tags": {
            str(item).strip().lower()
            for item in (raw.get("tags") or [])
            if str(item).strip()
        },
        "files": {
            str(item).strip()
            for item in (raw.get("files") or [])
            if str(item).strip()
        },
        "libraries": {
            str(item).strip()
            for item in (raw.get("libraries") or [])
            if str(item).strip()
        },
        "categories": {
            str(item).strip()
            for item in (raw.get("categories") or [])
            if str(item).strip()
        },
    }


def resolve_retrieval_scope_meta(
    *,
    tenant_id: str,
    source: str,
    tier: str,
    tenant_name: str = "",
    metadata: dict | None = None,
) -> dict:
    loaded_metadata = metadata if isinstance(metadata, dict) else load_knowledge_metadata(tenant_id, tenant_name)
    library_map = {str(item.get("library_id") or ""): dict(item) for item in loaded_metadata.get("libraries") or []}
    category_map = {str(item.get("category_id") or ""): dict(item) for item in loaded_metadata.get("categories") or []}
    clean_source = str(source or "").strip()
    clean_tier = str(tier or "").strip() or "permanent"
    source_name = clean_source.split("/", 1)[-1]
    metadata_items = loaded_metadata.get("items", {}) if isinstance(loaded_metadata.get("items"), dict) else {}
    meta = None
    for candidate in (clean_source, source_name, clean_source.split("/")[-1]):
        if not candidate:
            continue
        found_key = _find_metadata_key(metadata_items, clean_tier, candidate)
        if found_key in metadata_items:
            meta = metadata_items.get(found_key)
            break
    tags = list((meta or {}).get("tags") or [])
    library_id = str((meta or {}).get("library_id") or "").strip()
    category_id = str((meta or {}).get("category_id") or "").strip()
    public_tier = _public_tier_code(clean_tier)
    return {
        "tags": tags,
        "tier_code": public_tier,
        "file_key": f"{public_tier}/{source_name}",
        "library_id": library_id,
        "category_id": category_id,
        "library_name": str((library_map.get(library_id) or {}).get("name") or ""),
        "category_name": str((category_map.get(category_id) or {}).get("name") or ""),
    }


def retrieval_scope_meta_matches(scope_meta: dict, knowledge_scope: dict | None) -> bool:
    normalized_scope = _normalize_knowledge_scope(knowledge_scope)
    allowed_tiers = normalized_scope["tiers"]
    allowed_tags = normalized_scope["tags"]
    allowed_files = normalized_scope["files"]
    allowed_libraries = normalized_scope["libraries"]
    allowed_categories = normalized_scope["categories"]
    public_tier = str(scope_meta.get("tier_code") or "").strip()
    if allowed_tiers and public_tier not in allowed_tiers:
        return False
    library_id = str(scope_meta.get("library_id") or "").strip()
    if allowed_libraries and library_id not in allowed_libraries:
        return False
    category_id = str(scope_meta.get("category_id") or "").strip()
    if allowed_categories and category_id not in allowed_categories:
        return False
    if allowed_tags:
        tag_set = {
            str(tag).strip().lower()
            for tag in (scope_meta.get("tags") or [])
            if str(tag).strip()
        }
        if not (tag_set & allowed_tags):
            return False
    if allowed_files:
        source = str(scope_meta.get("source") or "").strip()
        source_name = source.split("/", 1)[-1]
        tier = str(scope_meta.get("tier") or "").strip() or "permanent"
        file_candidates = {
            source,
            source_name,
            str(scope_meta.get("file_key") or "").strip(),
            f"{tier}/{source_name}",
        }
        if not (file_candidates & allowed_files):
            return False
    return True


def annotate_retrieval_results_with_scope(
    *,
    tenant_id: str,
    tenant_name: str = "",
    results: list[dict] | None,
    knowledge_scope: dict | None,
) -> list[dict]:
    metadata = load_knowledge_metadata(tenant_id, tenant_name)
    filtered: list[dict] = []
    for item in list(results or []):
        source = str(item.get("source") or "").strip()
        tier = str(item.get("tier") or "").strip() or "permanent"
        scope_meta = resolve_retrieval_scope_meta(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            source=source,
            tier=tier,
            metadata=metadata,
        )
        enriched = {
            **dict(item),
            **scope_meta,
            "source": source,
            "tier": tier,
        }
        if not retrieval_scope_meta_matches(enriched, knowledge_scope):
            continue
        filtered.append(enriched)
    return filtered
