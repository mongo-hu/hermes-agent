"""Tests for DFM service fact value normalization.

Run with: python tests/dfm/test_service_normalization.py
"""

from tools.dfm.service import DFMService


def test_all_normalization():
    """Run all normalization tests."""
    service = DFMService()
    
    # Test 1: JSON string array
    result = service._normalize_fact_value("pull_dir", "[0, 0, 1]")
    assert result == [0, 0, 1] and isinstance(result, list), "Test 1 failed"
    
    # Test 2: JSON float array
    result = service._normalize_fact_value("pull_dir", "[0.0, 0.0, 1.0]")
    assert result == [0.0, 0.0, 1.0] and isinstance(result, list), "Test 2 failed"
    
    # Test 3: Already a list (should return same object)
    input_value = [0.0, 0.0, 1.0]
    result = service._normalize_fact_value("pull_dir", input_value)
    assert result == [0.0, 0.0, 1.0] and result is input_value, "Test 3 failed"
    
    # Test 4: Tuple (should return as-is)
    input_value = (0, 0, 1)
    result = service._normalize_fact_value("pull_dir", input_value)
    assert result == (0, 0, 1) and isinstance(result, tuple), "Test 4 failed"
    
    # Test 5: Non-normalized fact
    result = service._normalize_fact_value("material", "TPU")
    assert result == "TPU" and isinstance(result, str), "Test 5 failed"
    
    # Test 6: Invalid JSON string
    result = service._normalize_fact_value("pull_dir", "invalid json")
    assert result == "invalid json" and isinstance(result, str), "Test 6 failed"
    
    # Test 7: JSON with whitespace
    result = service._normalize_fact_value("pull_dir", "[ 0 , 0 , 1 ]")
    assert result == [0, 0, 1], "Test 7 failed"
    
    # Test 8: Negative values
    result = service._normalize_fact_value("pull_dir", "[-1, 0, 0]")
    assert result == [-1, 0, 0], "Test 8 failed"
    
    print("✅ All 8 tests passed!")


if __name__ == "__main__":
    test_all_normalization()
