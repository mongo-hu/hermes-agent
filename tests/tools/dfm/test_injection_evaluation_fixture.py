import itertools
import json
from pathlib import Path

from tools.dfm.ontology.store import LocalOntologyStore


FIXTURE_PATH = (
    Path(__file__).parents[2]
    / "fixtures"
    / "dfm"
    / "injection_two_feature_evaluation.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _rule_ids(row: dict) -> list[str]:
    if "rule_id" in row:
        return [row["rule_id"]]
    return list(row["rule_ids"])


def _factor_conditions(shared: dict, row: dict) -> list[dict]:
    conditions = []
    for factor_id, value in {**shared, **row}.items():
        if not factor_id.startswith("F_") or value == "*":
            continue
        conditions.append({
            "factor_id": factor_id,
            "operator": "IN" if isinstance(value, list) else "EQ",
            "value": value,
        })
    return conditions


def _selection_rows(shared: dict, cases: list[dict]) -> list[dict]:
    rows = []
    for row in cases:
        rule_ids = _rule_ids(row)
        excel_rows = list(row.get("excel_rows") or [row.get("excel_row", 0)])
        assert len(rule_ids) == len(excel_rows)
        for rule_id, excel_row in zip(rule_ids, excel_rows, strict=True):
            rows.append({
                "rule_id": rule_id,
                "conditions_json": json.dumps(
                    _factor_conditions(shared, row), ensure_ascii=False
                ),
                "priority": 10_000 - int(excel_row),
                "is_default": False,
                "threshold_json": "null",
                "expression_json": "null",
                "comparator": "",
            })
    return rows


def _concrete_facts(shared: dict, row: dict) -> list[dict]:
    fixed = dict(shared)
    alternatives: list[tuple[str, list[object]]] = []
    for factor_id, value in row.items():
        if not factor_id.startswith("F_") or value == "*":
            continue
        alternatives.append((
            factor_id,
            list(value) if isinstance(value, list) else [value],
        ))
    if not alternatives:
        return [fixed]
    return [
        {**fixed, **dict(zip((name for name, _ in alternatives), values, strict=True))}
        for values in itertools.product(*(values for _, values in alternatives))
    ]


def _question_tables(payload: dict):
    return (
        (
            "C_WALL_THK_RANGE",
            payload["main_wall"]["shared_facts"],
            payload["main_wall"]["thickness"],
        ),
        (
            "C_WALL_DRAFT",
            payload["main_wall"]["shared_facts"],
            payload["main_wall"]["draft"],
        ),
        (
            "C_SCREW_BOSS_WALL_THK",
            payload["screw_boss"]["shared_facts"],
            payload["screw_boss"]["wall_thickness"],
        ),
        (
            "C_SCREW_BOSS_BOTTOM_THK",
            payload["screw_boss"]["shared_facts"],
            payload["screw_boss"]["bottom_thickness"],
        ),
        (
            "C_SCREW_BOSS_FILLET",
            payload["screw_boss"]["shared_facts"],
            payload["screw_boss"]["fillet"],
        ),
    )


def test_every_concrete_answer_combination_selects_its_expected_rule():
    payload = _fixture()
    concrete_count = 0
    selectable_rule_ids = set()

    for check_id, shared, cases in _question_tables(payload):
        candidates = _selection_rows(shared, cases)
        for case in cases:
            expected = _rule_ids(case)
            assert len(expected) == 1
            selectable_rule_ids.update(expected)
            for facts in _concrete_facts(shared, case):
                concrete_count += 1
                selected = LocalOntologyStore._select_rule(candidates, facts, check_id)
                assert selected is not None, case["case_id"]
                assert selected["rule_id"] == expected[0], case["case_id"]

    assert concrete_count == payload["coverage"]["concrete_answer_combinations"]
    assert len(selectable_rule_ids) == payload["coverage"]["selectable_rule_ids"]
    assert set(payload["coverage"]["question_driven_checks"]) == {
        check_id for check_id, _, _ in _question_tables(payload)
    }


def test_cross_feature_cases_resolve_their_documented_rule_combination():
    payload = _fixture()
    tables = {
        check_id: _selection_rows(shared, cases)
        for check_id, shared, cases in _question_tables(payload)
    }
    shared_facts = {
        "F_PROC_BASE": "热塑性注塑",
        "F_PROC_AUX": "常规",
        "F_PROC_MULTI_MAT": "单材料",
    }

    assert (
        len(payload["cross_feature_cases"])
        == payload["coverage"]["representative_cross_feature_cases"]
    )
    for case in payload["cross_feature_cases"]:
        global_facts = {
            **shared_facts,
            **{key: value for key, value in case.items() if key.startswith("F_")},
        }
        selected = set()
        for check_id, candidates in tables.items():
            facts = {**global_facts, **case.get("check_facts", {}).get(check_id, {})}
            winner = LocalOntologyStore._select_rule(candidates, facts, check_id)
            if winner is not None:
                selected.add(winner["rule_id"])
        assert selected == set(case["expected_present_rule_ids"]), case["case_id"]


def test_excluded_rules_and_incomplete_checks_cannot_leak_into_expectations():
    payload = _fixture()
    excluded = {item["rule_id"] for item in payload["excluded_rules"]}
    expected = {
        rule_id
        for _, _, cases in _question_tables(payload)
        for case in cases
        for rule_id in _rule_ids(case)
    }
    expected.update(
        item["rule_id"]
        for section in (
            payload["main_wall"]["geometry"],
            payload["screw_boss"]["draft"],
        )
        for item in section
    )
    expected.update(
        payload["screw_boss"]["hole_depth"][0]["expected_rule_id"] for _ in [0]
    )

    assert not (excluded & expected)
    assert payload["excluded_checks"] == [
        {
            "check_id": "C_SCREW_BOSS_THIN_STEEL",
            "reason": "rule_library_execution_contract_incomplete",
            "policy": (
                "Do not ask F_TOOL_STEEL_TYPE and do not include thin-steel rules "
                "until the rule publication exposes a complete executable contract."
            ),
        }
    ]
    assert {
        case["check_facts"]["C_WALL_THK_RANGE"]["F_MAT_ADDITIVE_FILL"]
        for case in payload["cross_feature_cases"]
    } == {"非LGF", "LGF"}
    assert {
        case["check_facts"]["C_SCREW_BOSS_WALL_THK"]["F_MAT_ADDITIVE_FILL"]
        for case in payload["cross_feature_cases"]
        if "C_SCREW_BOSS_WALL_THK" in case.get("check_facts", {})
    } == {"未填充", "TD20、EPDM+TD10", "GF、LGF"}
