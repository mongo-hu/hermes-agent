import json
import os
from datetime import datetime, timezone
from typing import Optional
from .core_pipeline import process_file

def execute_2d_pipeline(file_path: str, quiet: bool = True) -> tuple[str, str]:
    """
    Executes the 2D pipeline and returns a strictly formatted JSONL string.
    This acts as the decoupled boundary interface.
    
    Args:
        file_path: Path to the 2D drawing file.
        quiet: If true, suppresses stdout from the underlying pipeline.
        
    Returns:
        A tuple of (jsonl_string, raw_text_string).
    """
    if not os.path.exists(file_path):
        error_event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "error",
            "message": f"File not found: {file_path}"
        }
        return json.dumps(error_event, ensure_ascii=False) + "\n", ""
        
    start_event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "progress",
        "stage": "started",
        "file": file_path
    }
    
    # Run the pipeline
    try:
        result, raw_text = process_file(file_path, quiet=quiet)
        
        result_event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "completed",
            "data": result
        }
        
        jsonl_str = json.dumps(start_event, ensure_ascii=False) + "\n" + json.dumps(result_event, ensure_ascii=False) + "\n"
        return jsonl_str, raw_text
        
    except Exception as e:
        error_event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "error",
            "message": str(e)
        }
        return json.dumps(error_event, ensure_ascii=False) + "\n", ""

# Allow running as a standalone script for subprocess usage
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        # Standard stdout output for subprocess integration
        jsonl, _ = execute_2d_pipeline(sys.argv[1], quiet=True)
        print(jsonl, end="")
