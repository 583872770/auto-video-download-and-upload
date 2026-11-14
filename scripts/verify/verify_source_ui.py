"""Run a legacy UI regression explicitly against the source-only fallback.

The built Vue UI has its own browser suites. This runner keeps fallback
coverage independent of whether local build artifacts happen to be present.
"""
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Usage: verify_source_ui.py scripts/verify/verify_<suite>.py [arguments]')
    script = Path(sys.argv[1]).resolve()
    allowed = Path(__file__).resolve().parent
    if script.parent != allowed or not script.is_file() or script == Path(__file__).resolve():
        raise SystemExit('Expected a verification script in scripts/verify')
    sys.argv = [str(script), *sys.argv[2:]]
    with patch('webapp.frontend.assets', return_value=None):
        runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
