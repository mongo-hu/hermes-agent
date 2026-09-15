import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
NATIVE = ROOT / "dfm-geometry"
pytestmark = pytest.mark.skipif(
    not (NATIVE / "CMakeLists.txt").is_file(),
    reason="external DFMAnalysis_OCCT checkout is not available",
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
    assert '"generator": "Ninja"' in presets
    assert "windows-vcpkg-vs2026-ninja-release" in presets
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
    assert "RUNTIME_DEPENDENCIES" in cmake
    assert "OpenCASCADE_BINARY_DIR" in cmake
    assert "${DFM_OCCT_TOOLKITS}" in cmake
    assert "${OpenCASCADE_LIBRARIES}" not in cmake.split("target_link_libraries", 1)[1]
    for toolkit in ("TKDESTEP", "TKXSBase", "TKBO", "TKFillet", "TKTopAlgo"):
        assert toolkit in cmake
    for forbidden_toolkit in ("TKV3d", "TKOpenGl", "TKMeshVS", "TKD3DHost"):
        assert forbidden_toolkit not in cmake


def test_step_import_is_strict_first_with_audited_fixshape_fallback():
    reader = (NATIVE / "src" / "io" / "step_reader.cpp").read_text(encoding="utf-8")
    engine = (NATIVE / "src" / "engine.cpp").read_text(encoding="utf-8")
    assert "reader.SetShapeProcessFlags(ShapeProcess::OperationsFlags{});" in reader
    assert "LoadedModel strict = transfer_step(path, true);" in reader
    assert "LoadedModel processed = transfer_step(path, false);" in reader
    assert '"shape_process_attempted", model.shape_process_attempted' in reader
    assert '"geometry_healing_applied", model.shape_process_attempted' in reader
    assert '"strict_validation", model.strict_validation' in reader
    assert '"post_shape_process_validation"' in reader
    assert 'case ShapeProcess::FixShape: return "FixShape";' in reader
    assert "ShapeFix_" not in reader
    assert 'invalid("empty_model"' in engine
    assert "detail::build_aag(model);" in engine
    assert engine.index("write_json_atomic(preflight_path") < engine.index(
        "detail::build_aag(model);"
    )
    assert "build_aag(model);" not in reader


def test_cli_routes_occt_default_messages_away_from_jsonl_stdout():
    cli = (NATIVE / "apps" / "cli" / "main.cpp").read_text(encoding="utf-8")
    assert "Message::DefaultMessenger()" in cli
    assert "messenger->ChangePrinters().Clear();" in cli
    assert 'new Message_PrinterOStream("cerr", Standard_False)' in cli


def test_engine_translates_occt_and_unknown_native_failures_to_jsonl_errors():
    engine = (NATIVE / "src" / "engine.cpp").read_text(encoding="utf-8")
    assert "catch (const Standard_Failure &failure)" in engine
    assert "catch (...)" in engine
    assert engine.count('emit(event_stream, "error"') >= 4


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
