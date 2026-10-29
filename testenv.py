"""Import first in every test: a temp config folder with the example policy, never the live config or ~/.claude."""
import os
import shutil
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp()).resolve()
os.environ["TECHICHI_HOME"] = str(HOME)
os.environ["CLAUDE_CONFIG_DIR"] = str(HOME / "claude")
shutil.copy(Path(__file__).resolve().parent / "policy.example.toml", HOME / "policy.toml")
