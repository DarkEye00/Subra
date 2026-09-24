import sys
from pathlib import Path

# Make tests/deckbuilder.py importable and the repo root importable.
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
