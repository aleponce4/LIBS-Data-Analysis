"""ProLIBSpector Public Edition main entry point."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is on python path when running directly from root
SRC_PATH = Path(__file__).resolve().parent / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from prolibspector.app.bootstrap import main

if __name__ == "__main__":
    main()
