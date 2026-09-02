import sys
from pathlib import Path

# v2/*.py modules use bare sibling imports (e.g. `from config import ...`),
# relying on the interpreter adding the script's own directory to sys.path
# when run directly (`python3 v2/daemon.py`). pytest doesn't do that, so
# point it at v2/ explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
