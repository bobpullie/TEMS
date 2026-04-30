"""Entrypoint for `python -m tems`.

Allows invocation via the canonical Python module syntax in addition to the
console script registered as `tems` in pyproject.toml. Useful when:
- the console script is not on PATH (Windows venv quirks)
- the user wants to be explicit about which interpreter runs the CLI
  (e.g. `py -3.13 -m tems scaffold`)
"""

from tems.cli import main

if __name__ == "__main__":
    main()
