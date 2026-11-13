"""Surface edits invalidate declared consumers while preserving other geometry."""

from dataclasses import replace

from tools.dfm.contracts import (
    GeometricFindingRecord, GeometricRequest, PlanFragmentRecord, PlanRecord, ProjectManifest,
)
from tools.dfm.service import DFMService


def test_annotation_revisions_invalidate_only_declared_consumers_and_findings():
    def fragment(identity, geometric_id):
        return PlanFragmentRecord(identity, "snapshot", "discovery", "worker", "a" * 64,
                                  [], [], [GeometricRequest("wall", [geometric_id], {})])

    def finding(identity, geometric_id):
        return GeometricFindingRecord(identity, identity, "unclassified", "open", [], [],
                                      [geometric_id], [], [], "review", feature_refs=["wall"])

    appearance = fragment("appearance", "G_WALL_APPEARANCE_DRAFT")
    thickness = fragment("thickness", "G_WALL_THK_MIN")
    manifest = ProjectManifest("project", "annotations", "now", "now", plans=[
        PlanRecord("appearance", "step", ["occt_cpp"], "ready", "now", plan_fragments=[appearance]),
        PlanRecord("thickness", "step", ["occt_cpp"], "ready", "now", plan_fragments=[thickness]),
    ], findings=[finding("appearance", "G_WALL_APPEARANCE_DRAFT"), finding("thickness", "G_WALL_THK_MIN")],
        capabilities={"geometry_discovery": {"geometry_capability": {"geometrics": [
            {"geometric_id": "G_WALL_APPEARANCE_DRAFT", "support_status": "supported", "requires_annotations": ["appearance"]},
            {"geometric_id": "G_WALL_THK_MIN", "support_status": "supported"},
        ]}}})
    revised = DFMService._invalidate_plans_for_semantics(manifest, "annotation:appearance")
    assert [plan.status for plan in revised] == ["invalidated", "ready"]
    findings = DFMService._invalidate_findings_for_semantics(manifest, "annotation:appearance")
    assert [finding.validity_status for finding in findings] == ["stale", "current"]
    assert DFMService._invalidate_plans_for_semantics(manifest, "annotation:parting_region") == manifest.plans
    assert DFMService._invalidate_findings_for_semantics(manifest, "annotation:parting_region") == manifest.findings
    old_worker = replace(manifest, capabilities={})
    assert DFMService._invalidate_plans_for_semantics(old_worker, "annotation:appearance") == manifest.plans
