"""A failed review save blocks the entire confirmation, not individual dependants."""

from dataclasses import replace
from datetime import datetime, timezone

from ..errors import DFMError, ManifestCommitError, ReviewSaveError


EDIT_ACTIONS = frozenset({
    "revise_annotations", "update_forming_scope_geometry", "restructure_forming_scopes",
    "analyze_forming_scope", "revise_forming_scopes", "stage_forming_direction",
    "update_feature_geometry", "confirm_discovery", "remove_review_group",
})


SAVE_FAILURES = (ManifestCommitError, ReviewSaveError)


def reconcile_legacy_confirmation_failure(store):
    """Recover only a provably committed confirmation misclassified by older code.

    Real failed/pending geometry intents and classified save failures are never
    cleared. The historical failure is retained as an auditable recovery record.
    """
    def recoverable(current):
        failure = current.capabilities.get("review_save_failure") or {}
        identity = failure.get("identity") or []
        if (failure.get("failure_category") or failure.get("reason") != "review_group_stale"
            or len(identity) != 2 or identity[0] != "confirm_discovery" or not identity[1]
            or any(edit.get("status") in {"pending", "failed"} for edit in current.geometry_edits)):
            return False
        confirmed = {f"{group.kind}:{ref}" for group in current.review_groups
                     if group.status == "confirmed" for ref in group.target_refs}
        return set(identity[1]) <= confirmed

    manifest = store.load()
    if not recoverable(manifest):
        return manifest

    def recover(current):
        if not recoverable(current):
            return current
        capabilities = dict(current.capabilities)
        failure = capabilities.pop("review_save_failure")
        capabilities["review_save_recovery"] = {
            "reason": "confirmation_already_committed", "previous_failure": failure,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
        }
        return replace(current, capabilities=capabilities)

    return store.update(recover)


def save_identity(action, params, manifest=None):
    # Identity of the user's edit, excluding values they must be able to correct.
    targets = [str(params[key]) for key in ("feature_id", "forming_scope_id") if params.get(key)]
    for collection, key in (("forming_scopes", "forming_scope_id"), ("annotations", "annotation_id")):
        targets.extend(str(row.get(key, "")) for row in (params.get(collection) or []) if isinstance(row, dict))
    groups = {row.review_group_id: row for row in manifest.review_groups} if manifest else {}
    review_ids = list(params.get("review_group_ids") or [])
    if params.get("review_group_id"):
        review_ids.append(params["review_group_id"])
    for value in review_ids:
        group = groups.get(value)
        if group:
            targets.extend(f"{group.kind}:{ref}" for ref in group.target_refs)
        else:
            targets.append(str(value))
    for key in ("retire_scope_ids", "retire_annotation_ids"):
        targets.extend(str(value) for value in params.get(key, []) or [])
    scope = params.get("forming_scope")
    if isinstance(scope, dict):
        targets.append(str(scope.get("forming_scope_id", "")))
    return [action, sorted(set(targets))]


def require_saved_review(manifest):
    barrier = manifest.capabilities.get("review_save_failure")
    if barrier:
        raise DFMError("review_save_failed", "Review saving failed. Correct and retry that save before analysis or reporting.", barrier)
    if any(row.get("status") == "pending" for row in manifest.geometry_edits):
        raise DFMError("review_save_pending", "Wait for the review save to finish before continuing.")
