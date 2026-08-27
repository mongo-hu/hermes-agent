import pytest

from tools.dfm.config import DFMConfig, load_dfm_config
from tools.dfm.errors import DFMError


def test_dfm_config_defaults_match_m0_contract():
    config = load_dfm_config({})

    assert config == DFMConfig(
        runtime_python="auto",
        default_process="injection",
        max_concurrent_runs=1,
        timeout_seconds=900,
        max_file_size_mb=200,
        max_pages=50,
        keep_failed_runs=True,
        max_evidence_findings=12,
    )


def test_dfm_config_reads_nested_values():
    config = load_dfm_config(
        {
            "dfm": {
                "runtime": {
                    "python": "C:/dfm/python.exe",
                    "max_concurrent_runs": 2,
                    "timeout_seconds": 120,
                },
                "intake": {"max_file_size_mb": 12, "max_pages": 8},
                "defaults": {"process": "injection"},
                "retention": {"keep_failed_runs": False},
                "evidence": {"max_rendered_findings": 7},
                "nx": {
                    "endpoint": "https://nx.example.internal/",
                    "request_timeout_seconds": 15,
                    "poll_interval_seconds": 1,
                },
            }
        }
    )

    assert config.runtime_python == "C:/dfm/python.exe"
    assert config.default_process == "injection"
    assert config.max_concurrent_runs == 2
    assert config.timeout_seconds == 120
    assert config.max_file_size_mb == 12
    assert config.max_pages == 8
    assert config.keep_failed_runs is False
    assert config.max_evidence_findings == 7
    assert config.nx_endpoint == "https://nx.example.internal"
    assert config.nx_request_timeout_seconds == 15
    assert config.nx_poll_interval_seconds == 1


def test_dfm_config_reads_ontology_sync_contract():
    config = load_dfm_config(
        {
            "dfm": {
                "ontology": {
                    "endpoint": "https://dfm.example/",
                    "process": "injection",
                    "organization_id": "org-1",
                    "sync_interval_seconds": 60,
                    "request_timeout_seconds": 10,
                    "pinned_snapshot_id": "ontology.injection@2",
                }
            }
        }
    )

    assert config.ontology_endpoint == "https://dfm.example"
    assert config.ontology_organization_id == "org-1"
    assert config.ontology_sync_interval_seconds == 60
    assert config.ontology_pinned_snapshot_id == "ontology.injection@2"


def test_dfm_endpoint_enables_ontology_sync_without_an_extra_switch():
    config = load_dfm_config(
        {"dfm": {"ontology": {"endpoint": "http://127.0.0.1:8000"}}}
    )

    assert config.ontology_endpoint == "http://127.0.0.1:8000"


def test_dfm_config_normalizes_the_m0_process_name():
    config = load_dfm_config({"dfm": {"defaults": {"process": "injection_molding"}}})

    assert config.default_process == "injection"


@pytest.mark.parametrize(
    "mapping",
    [
        {"dfm": {"runtime": {"max_concurrent_runs": 0}}},
        {"dfm": {"runtime": {"timeout_seconds": "slow"}}},
        {"dfm": {"intake": {"max_file_size_mb": -1}}},
        {"dfm": {"retention": {"keep_failed_runs": "yes"}}},
        {"dfm": {"evidence": {"max_rendered_findings": 0}}},
    ],
)
def test_dfm_config_rejects_invalid_values(mapping):
    with pytest.raises(DFMError) as exc_info:
        load_dfm_config(mapping)

    assert exc_info.value.code == "config_invalid"
