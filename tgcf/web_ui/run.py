import os
import subprocess
import sys
from pathlib import Path

from tgcf.config import CONFIG

# Keep module-level path for other web_ui modules that import it.
package_dir = str(Path(__file__).resolve().parent)

def main():
    path = os.path.join(package_dir, "0_👋_Hello.py")
    os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme
    os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    os.environ["STREAMLIT_SERVER_HEADLESS"] = "true"
    cmd = [sys.executable, "-m", "streamlit", "run", path]
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        return
