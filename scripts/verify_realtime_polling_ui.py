"""Compatibility entry point; implementation lives in scripts/verify/."""
from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("verify") / "verify_realtime_polling_ui.py"), run_name="__main__")
else:
    from scripts.verify.verify_realtime_polling_ui import *  # noqa: F401,F403
