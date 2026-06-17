from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app_config import load_app_config
from backend.release_profiles import export_all_release_bundles


def main() -> None:
    results = export_all_release_bundles(current_app_config=load_app_config())
    payload = [
        {
            "profile_key": item["profile"]["key"],
            "label": item["profile"]["label"],
            "bundle_dir": item["bundle_dir"],
            "zip_name": item["zip_name"],
            "zip_path": item["zip_path"],
        }
        for item in results
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
