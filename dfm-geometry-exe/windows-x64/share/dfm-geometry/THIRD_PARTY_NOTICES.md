# Third-Party Notices

This project incorporates components from the projects listed below.

---

## Open CASCADE Technology (OCCT 7.9.3)

- **License**: LGPL-2.1-or-later with OCCT LGPL exception
- **Copyright**: Open CASCADE S.A.S.
- **Source**: <https://dev.opencascade.org/>
- **Source revision used by the locked vcpkg baseline**:
  `5bd1466f08e98422f72901d1fb36d308854e0367`
- **License text**: [licenses/LGPL-2.1-or-later.txt](licenses/LGPL-2.1-or-later.txt),
  [licenses/OCCT_LGPL_EXCEPTION.txt](licenses/OCCT_LGPL_EXCEPTION.txt)

## Analysis Situs (v2025.2)

- **License**: BSD-3-Clause
- **Copyright**: Sergey Slyadnev and contributors
- **Source**: <https://analysissitus.org/>
- **Pinned upstream release / commit**: `v2025.2` /
  `aa5958932c8c85c068566ab685f2b99c0436b926`
- **License text**: [licenses/AnalysisSitus-BSD-3-Clause.txt](licenses/AnalysisSitus-BSD-3-Clause.txt)

The native engine links the source-built `asiAlgo` and `asiActiveData`
libraries. Its feature-recognition path uses `asiAlgo_AAG`,
`asiAlgo_RecognizeDrillHoles`, `asiAlgo_RecognizeBlends`,
`asiAlgo_RecognizeShafts`, `asiAlgo_RecognizeCavities`,
`asiAlgo_RecognizeConvexHull`, `asiAlgo_RecognizeIsolated`,
`asiAlgo_RecognizeProbes`, and `asiAlgo_RecognizeCanonical`.

The DFM metric implementations are clean-room C++ ports whose algorithmic
provenance was reviewed against the project references `draft_analyzer.py`,
`ray_thickness_analyzer.py`, `sphere_thickness_analyzer.py`,
`undercut_analyzer.py`, and `sharp_corners.py`. These filenames identify the
reviewed upstream reference material; no Python or PythonOCC runtime is
embedded or invoked by this project.

## Eigen3

- **License**: MPL-2.0
- **Source**: <https://eigen.tuxfamily.org/>

## nlohmann/json

- **License**: MIT
- **Copyright**: Niels Lohmann
- **Source**: <https://github.com/nlohmann/json>

## RapidJSON

- **License**: MIT
- **Copyright**: THL A29 Limited, a Tencent company, and Milo Yip
- **Source**: <https://github.com/Tencent/rapidjson>

## Catch2

- **License**: BSL-1.0
- **Source**: <https://github.com/catchorg/Catch2>

## Three.js (Web viewer, 0.183.1)

- **License**: MIT
- **Copyright**: 2010-2026 three.js authors
- **Source**: <https://github.com/mrdoob/three.js>
- **License text**: [licenses/Three.js-MIT.txt](licenses/Three.js-MIT.txt)

The Web viewer resolves Three.js through the version pinned in
`web/package-lock.json`; its generated `node_modules/` directory is not committed.
