import sys
import os

# Add project directory to Python path
project_dir = os.path.abspath(os.path.dirname(__file__))
if project_dir not in sys.path:
    sys.path.append(project_dir)
    print(f'Added project directory to Python path: {project_dir}')

# Force UTF-8 stdout/stderr to avoid cp950/console encoding errors on Windows
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# Try to import and run web_ui.run
if __name__ == '__main__':
    try:
        from tgcf.web_ui import run
        print('Successfully imported tgcf.web_ui.run module')
        run.main()
    except ImportError as e:
        print(f'Failed to import tgcf.web_ui.run: {e}')
        sys.exit(1)
    except Exception as e:
        print(f'Error while running web UI: {e}')
        sys.exit(1)