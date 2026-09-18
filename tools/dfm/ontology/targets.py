"""Bind published comparison text to existing Discovery scopes, never invent faces.

Only bounded role vocabulary and explicit Discovery links are executable. Unknown
or ambiguous text fails closed and reports the Check/alias to the caller.
"""

import re

from ..errors import DFMError


_ROLE_TERMS = (
    ({"outer_side", "outer_wall"}, ("outer", "外侧", "外壁", "外表面")),
    ({"inner_side", "inner_wall"}, ("inner", "内侧", "内壁", "内表面")),
    ({"root", "transition"}, ("root", "根部", "过渡")),
    ({"bottom"}, ("bottom", "底部", "底面")),
    ({"top"}, ("top", "顶部", "顶面")),
    ({"hole", "cylindrical_wall"}, ("hole", "孔")),
    ({"wall"}, ("wall", "壁厚", "柱壁", "主体壁")),
)


def _scope_error(spec, anchor, candidates, message):
    raise DFMError(
        "analysis_operand_ambiguous",
        message,
        {
            "check_id": spec["check_id"],
            "operand_alias": spec["alias"],
            "operand_text": spec["operand_text"],
            "anchor_feature_id": anchor.feature_id,
            "candidate_region_ids": [item.region_id for item in candidates],
        },
    )


def _mentions(text, term):
    if term.isascii():
        return (
            re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text) is not None
        )
    return term in text


def _select_regions(spec, anchor, features, regions):
    text = spec["operand_text"].strip().lower()
    candidates = [
        item
        for item in regions
        if anchor.feature_id in item.feature_refs
        and item.input_sha256 == anchor.input_sha256
    ]
    adjacent = any(_mentions(text, term) for term in ("adjacent", "相邻", "邻近"))
    if adjacent:
        direct = [item for item in candidates if item.role == "adjacent_main_wall"]
        if len(direct) == 1:
            return direct
        local_refs = set()
        for link in anchor.relationships:
            target = features.get(link.get("target_ref"))
            if (
                link.get("relation") in {"adjacent_to", "attached_to"}
                and target is not None
                and target.kind == "main_wall"
                and target.input_sha256 == anchor.input_sha256
            ):
                local_refs.update(link.get("region_refs", []))
        candidates = [
            item
            for item in regions
            if item.region_id in local_refs
            and item.input_sha256 == anchor.input_sha256
            and any(
                features.get(ref) is not None and features[ref].kind == "main_wall"
                for ref in item.feature_refs
            )
        ]
        if len(candidates) != 1:
            _scope_error(
                spec,
                anchor,
                candidates,
                "Adjacent wall measurements require one explicit local Discovery region link; whole-part fallback is not allowed.",
            )
        return candidates
    exact = [item for item in candidates if item.semantic_label.strip().lower() == text]
    if len(exact) == 1:
        return exact
    if anchor.kind == "ordinary_part" and (
        "普通主体" in text or _mentions(text, "ordinary")
    ):
        selected = [item for item in candidates if item.role == "ordinary"]
    else:
        matches = [
            roles
            for roles, terms in _ROLE_TERMS
            if any(_mentions(text, term) for term in terms)
        ]
        specific = [roles for roles in matches if roles != {"wall"}]
        if len(specific) > 1:
            _scope_error(
                spec,
                anchor,
                candidates,
                "Comparison text names multiple geometric roles and requires an explicit Discovery scope.",
            )
        roles = (specific or matches or [set()])[0]
        selected = [item for item in candidates if item.role in roles]
    if len(selected) != 1:
        _scope_error(
            spec,
            anchor,
            candidates,
            "Comparison text does not resolve to one existing Discovery region.",
        )
    return selected


def resolve_catalog_targets(specs, manifest, snapshot):
    features = [
        item for item in manifest.features if item.feature_id in snapshot.feature_refs
    ]
    regions = [
        item for item in manifest.regions if item.region_id in snapshot.region_refs
    ]
    return resolve_catalog_scopes(specs, features, regions)


def resolve_catalog_scopes(specs, discovered_features, regions):
    features = {
        item.feature_id: item
        for item in discovered_features
        if item.status in {"confirmed", "detected"}
    }
    targets = {}
    for spec in specs:
        for anchor in features.values():
            if anchor.kind not in spec["feature_kinds"]:
                continue
            if anchor.kind == "ordinary_part" and any(
                item.kind != "ordinary_part"
                and item.kind in spec["feature_kinds"]
                and item.input_sha256 == anchor.input_sha256
                for item in features.values()
            ):
                continue
            for region in _select_regions(spec, anchor, features, regions):
                owners = [
                    features[ref] for ref in region.feature_refs if ref in features
                ]
                if len(owners) != 1:
                    _scope_error(
                        spec,
                        anchor,
                        [region],
                        "An executable region must have one discovered Feature owner.",
                    )
                key = (spec["metric_id"], region.region_id)
                target = targets.setdefault(
                    key,
                    {
                        "feature": owners[0],
                        "region": region,
                        "metric_id": spec["metric_id"],
                        "operand_anchors": {},
                    },
                )
                operand_key = (spec["check_id"], spec["alias"])
                anchors = target["operand_anchors"].setdefault(operand_key, [])
                if anchor.feature_id not in anchors:
                    anchors.append(anchor.feature_id)
    return list(targets.values())
