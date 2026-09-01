import os
import sys

# Change working dir to project directory
project_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_dir)

from core.watcher import run_watcher

if __name__ == "__main__":
    run_watcher()
