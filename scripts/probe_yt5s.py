"""Run the same native-browser resolver used by download method two."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from webapp.yt5s import main

if __name__ == '__main__':
    raise SystemExit(main())
