#!/usr/bin/env python3
# launcher script

import subprocess
import sys


def install_deps():
    # check and install requirements
    try:
        with open("requirements.txt") as f:
            reqs = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        print("requirements.txt not found")
        return False
    
    # quick check
    result = subprocess.run(
        [sys.executable, "-m", "pip", "list", "--format=freeze"],
        capture_output=True, text=True
    )
    installed = {line.split("==")[0].lower() for line in result.stdout.splitlines()}
    
    missing = []
    for req in reqs:
        pkg = req.split("==")[0].split(">=")[0].split("<=")[0].strip().lower()
        if pkg not in installed:
            missing.append(req)
    
    if missing:
        print(f"installing {len(missing)} missing packages...")
        for pkg in missing:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"])
        print("done")
    
    return True


if __name__ == "__main__":
    if install_deps():
        import main
        main.run()
    else:
        print("failed to setup")
        sys.exit(1)