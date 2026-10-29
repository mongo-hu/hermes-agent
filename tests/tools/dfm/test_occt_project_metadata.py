import hashlib
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


def _native_project_root() -> Path | None:
    candidates: list[Path] = []
    schema_root = os.environ.get("DFM_GEOMETRY_SCHEMA_ROOT")
    if schema_root:
        candidates.append(Path(schema_root).expanduser().resolve().parent)

    executable = os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE")
    if executable:
        executable_path = Path(executable).expanduser().resolve()
        candidates.extend(executable_path.parents)

    candidates.extend(
        (
            ROOT.parent.parent / "模具项目" / "occt-worker",
            ROOT.parent / "occt-worker",
            ROOT.parent.parent / "occt-worker",
        )
    )
    return next(
        (candidate for candidate in candidates if (candidate / "CMakeLists.txt").is_file()),
        None,
    )


NATIVE = _native_project_root()
pytestmark = pytest.mark.skipif(
    NATIVE is None,
    reason="standalone occt-worker checkout is not available",
)


def test_vcpkg_manifest_locks_occt_793_and_dynamic_triplet():
    manifest = json.loads((NATIVE / "vcpkg.json").read_text(encoding="utf-8"))
    assert manifest["license"] == "LGPL-2.1-or-later"
    assert manifest["builtin-baseline"] == "4f6d4ae8247b2dcae554555a135e52bb449dd524"
    assert {item if isinstance(item, str) else item["name"] for item in manifest["dependencies"]} == {
        "catch2",
        "eigen3",
        "nlohmann-json",
        "opencascade",
        "rapidjson",
    }
    assert manifest["overrides"] == [
        {"name": "opencascade", "version": "7.9.3", "port-version": 1}
    ]
    presets = (NATIVE / "CMakePresets.json").read_text(encoding="utf-8")
    bootstrap = (NATIVE / "scripts" / "bootstrap-windows.ps1").read_text(
        encoding="utf-8"
    )
    assert '"VCPKG_TARGET_TRIPLET": "x64-windows"' in presets
    assert "x64-windows-static" not in presets
    assert '"generator": "Visual Studio 18 2026"' in presets
    assert "windows-vcpkg-vs2026-sln" in presets
    assert "status --porcelain --untracked-files=all" in bootstrap
    assert "fetch --depth" not in bootstrap
    assert "fetch --unshallow origin" in bootstrap
    assert "Visual Studio 2022 Build Tools are required" in bootstrap
    assert "VCPKG_VISUAL_STUDIO_PATH" in bootstrap
    assert "VisualStudioRoot" in bootstrap


def test_native_project_has_exact_targets_and_occt_version():
    cmake = (NATIVE / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "find_package(OpenCASCADE 7.9.3 EXACT CONFIG REQUIRED)" in cmake
    assert "requires a dynamically linked OCCT 7.9.3 installation" in cmake
    assert "add_library(dfm_geometry_core STATIC" in cmake
    assert "add_executable(dfm-geometry" in cmake
    assert "add_executable(dfm_geometry_tests" in cmake
    assert "include(cmake/AnalysisSitus.cmake)" in cmake
    assert "asiAlgo" in cmake
    assert "asiActiveData" in cmake
    assert "target_link_options(dfm-geometry PRIVATE /STACK:8388608)" in cmake
    assert "target_link_options(dfm_geometry_tests PRIVATE /STACK:8388608)" in cmake
    assert "cmake_policy(SET CMP0207 NEW)" in cmake
    assert "DFM_APPLOCAL_RUNTIME_NAMES" in cmake
    assert "copy_if_different" in cmake
    assert "OpenCASCADE_BINARY_DIR" in cmake
    assert "${DFM_OCCT_TOOLKITS}" in cmake
    assert "${OpenCASCADE_LIBRARIES}" not in cmake.split("target_link_libraries", 1)[1]
    for toolkit in ("TKDESTEP", "TKXSBase", "TKBO", "TKFillet", "TKTopAlgo"):
        assert toolkit in cmake
    for forbidden_toolkit in ("TKV3d", "TKOpenGl", "TKMeshVS", "TKD3DHost"):
        assert forbidden_toolkit not in cmake


def test_step_import_is_strict_first_with_audited_fixshape_fallback():
    reader = (NATIVE / "src" / "geometry" / "io" / "step_reader.cpp").read_text(
        encoding="utf-8"
    )
    normalizer = (
        NATIVE / "src" / "geometry" / "healing" / "step_shape_normalizer.cpp"
    ).read_text(encoding="utf-8")
    preflight = (NATIVE / "src" / "artifacts" / "preflight_builder.cpp").read_text(
        encoding="utf-8"
    )
    geometry_context = (
        NATIVE / "src" / "geometry" / "geometry_context.cpp"
    ).read_text(encoding="utf-8")
    session = (NATIVE / "src" / "application" / "session_service.cpp").read_text(encoding="utf-8")
    assert "reader.SetShapeProcessFlags(ShapeProcess::OperationsFlags{});" in reader
    assert "model.strict_validation = validation_summary(model);" in reader
    assert "return normalize_step_shape(std::move(model), parameters);" in reader
    assert "BRepBuilderAPI_Copy(model.shape, true, false)" in normalizer
    assert 'model.shape_process_operations.push_back("FixShape")' in normalizer
    assert "model.post_shape_process_validation = validation_summary(model);" in normalizer
    assert '"shape_process_attempted", model.shape_process_attempted' in preflight
    assert '"geometry_healing_applied", model.shape_process_attempted' in preflight
    assert '"strict_validation", model.strict_validation' in preflight
    assert '"post_shape_process_validation", model.post_shape_process_validation' in preflight
    assert "if (model_.aag.IsNull())" in geometry_context
    assert "build_aag(model_);" in geometry_context
    assert "make_preflight(" in session
    assert "build_aag" not in reader


def test_cli_routes_occt_default_messages_away_from_jsonl_stdout():
    cli = (NATIVE / "apps" / "cli" / "main.cpp").read_text(encoding="utf-8")
    assert "Message::DefaultMessenger()" in cli
    assert "messenger->ChangePrinters().Clear();" in cli
    assert 'new Message_PrinterOStream("cerr", Standard_False)' in cli


@pytest.mark.skipif(not os.environ.get("DFM_GEOMETRY_E2E_EXECUTABLE"), reason="native Worker executable is not configured")
def test_engine_translates_native_failures_to_json_rpc_errors(tmp_path):
    from tools.dfm.errors import DFMError
    from tools.dfm.runtime.process import GeometrySession

    session = GeometrySession(os.environ["DFM_GEOMETRY_E2E_EXECUTABLE"], request_timeout_seconds=30)
    try:
        pid = session.pid
        with pytest.raises(DFMError) as failure:
            session.call("snapshots.open", {"input_path": str(tmp_path / "missing.step"),
                "input_sha256": "a" * 64, "artifact_dir": str(tmp_path / "geometry"), "lease_seconds": 900})
        assert failure.value.code == "geometry_input_invalid"
        assert session.call("capabilities.get", {})["contract_version"] == "dfm.geometry/v2"
        assert session.alive and session.pid == pid
    finally:
        session.close()


def test_licenses_and_upstream_provenance_are_pinned():
    notice = (NATIVE / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    required = {
        "5bd1466f08e98422f72901d1fb36d308854e0367",
        "v2025.2",
        "aa5958932c8c85c068566ab685f2b99c0436b926",
        "draft_analyzer.py",
        "ray_thickness_analyzer.py",
        "sphere_thickness_analyzer.py",
        "undercut_analyzer.py",
        "sharp_corners.py",
        "asiAlgo_AAG",
        "asiAlgo_RecognizeDrillHoles",
        "asiAlgo_RecognizeBlends",
        "asiAlgo_RecognizeShafts",
        "asiAlgo_RecognizeCavities",
        "asiAlgo_RecognizeConvexHull",
        "asiAlgo_RecognizeIsolated",
        "asiAlgo_RecognizeProbes",
        "asiAlgo_RecognizeCanonical",
    }
    assert all(item in notice for item in required)
    assert "SPDX-License-Identifier: LGPL-2.1-or-later" in (
        NATIVE / "LICENSE"
    ).read_text(encoding="utf-8")
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in (
        NATIVE / "licenses" / "LGPL-2.1-or-later.txt"
    ).read_text(encoding="utf-8")
    occt_exception = (
        NATIVE / "licenses" / "OCCT_LGPL_EXCEPTION.txt"
    ).read_text(encoding="utf-8")
    assert "Open CASCADE exception (version 1.0)" in occt_exception
    assert "prominent notice in supporting documentation" in occt_exception
    assert "Redistribution and use in source and binary forms" in (
        NATIVE / "licenses" / "AnalysisSitus-BSD-3-Clause.txt"
    ).read_text(encoding="utf-8")
    assert not (NATIVE / "licenses" / "Palmetto-MIT.txt").exists()


def test_analysis_situs_is_source_built_and_excluded_geometry_stacks_stay_out():
    dependency_text = "\n".join(
        [
            (NATIVE / "CMakeLists.txt").read_text(encoding="utf-8"),
            (NATIVE / "vcpkg.json").read_text(encoding="utf-8"),
            (NATIVE / "cmake" / "AnalysisSitus.cmake").read_text(encoding="utf-8"),
        ]
    ).lower()
    assert "analysis situs" in dependency_text
    assert "add_subdirectory" in dependency_text
    assert (NATIVE / "third_party" / "analysis_situs" / "src" / "asiAlgo").is_dir()
    assert (NATIVE / "third_party" / "analysis_situs" / "src" / "asiActiveData").is_dir()
    for forbidden in (
        "cadquery",
        "embree",
        "freecad",
        "gmsh",
        "pythonocc",
        "tetgen",
    ):
        assert forbidden not in dependency_text


def test_windows_runtime_bundle_is_complete_and_hash_locked():
    runtime = NATIVE / "runtime" / "windows-x64"
    expected = {
        "TKBin.dll",
        "TKBinL.dll",
        "TKBO.dll",
        "TKBool.dll",
        "TKBRep.dll",
        "TKCAF.dll",
        "TKCDF.dll",
        "TKDE.dll",
        "TKDEIGES.dll",
        "TKDESTEP.dll",
        "TKDESTL.dll",
        "TKernel.dll",
        "TKFillet.dll",
        "TKG2d.dll",
        "TKG3d.dll",
        "TKGeomAlgo.dll",
        "TKGeomBase.dll",
        "TKHLR.dll",
        "TKLCAF.dll",
        "TKMath.dll",
        "TKMesh.dll",
        "TKOffset.dll",
        "TKOpenGl.dll",
        "TKPrim.dll",
        "TKService.dll",
        "TKShHealing.dll",
        "TKTopAlgo.dll",
        "TKV3d.dll",
        "TKVCAF.dll",
        "TKXCAF.dll",
        "TKXSBase.dll",
        "msvcp140.dll",
        "vcomp140.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
    }
    assert {path.name for path in runtime.glob("*.dll")} == expected
    assert not list(runtime.glob("*.exe"))
    assert not list(runtime.glob("*.lib"))
    assert "asiAlgo.dll" not in expected
    assert "asiActiveData.dll" not in expected

    manifest = {}
    for line in (runtime / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split(maxsplit=1)
        manifest[filename] = digest
    assert set(manifest) == expected
    for filename, expected_digest in manifest.items():
        actual = hashlib.sha256((runtime / filename).read_bytes()).hexdigest()
        assert actual == expected_digest

    cmake = (NATIVE / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "DFM_USE_BUNDLED_WINDOWS_RUNTIME" in cmake
    assert "dfm_validate_runtime_bundle" in cmake
    assert "SHA256SUMS.txt" in cmake
