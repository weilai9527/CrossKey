"""Run the suite and expose actionable CI failure annotations on public checks."""
from __future__ import annotations

import os
import sys
import unittest


def annotation(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    suite = unittest.defaultTestLoader.discover(".")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for test, details in result.failures + result.errors:
            print(f"::error title={annotation(str(test))}::{annotation(details)}", flush=True)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
