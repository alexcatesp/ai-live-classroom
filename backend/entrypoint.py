"""PyInstaller entry point.

The frozen executable cannot use `aiclassroom/main.py` directly as its script:
PyInstaller runs it as a top-level module, so the relative imports inside the
package fail. This wrapper imports the package properly instead.
"""

from aiclassroom.main import main

if __name__ == "__main__":
    raise SystemExit(main())
