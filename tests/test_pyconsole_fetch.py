"""The live web console's Pyodide loader must fetch every vendored module.

docs/console/py/ holds byte-copies of the modules checks.py imports at
import time (checks, verdict, budget, transport). If pyconsole.js forgets to
fetch one of them (see #549 for transport.py), importing webshim fails in
the browser with ImportError even though it passes on disk where all files
sit together. This test pins the fetch/write list in pyconsole.js to the
actual contents of docs/console/py/ so a future miss fails CI.
"""

import os
import re

CONSOLE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "docs", "console"))
PY_DIR = os.path.join(CONSOLE_DIR, "py")


def _read_pyconsole():
    with open(os.path.join(CONSOLE_DIR, "pyconsole.js"), encoding="utf-8") as fh:
        return fh.read()


def _vendored_py_modules():
    return sorted(name for name in os.listdir(PY_DIR) if name.endswith(".py"))


def test_pyconsole_fetches_every_vendored_module():
    js = _read_pyconsole()
    fetched = sorted(set(re.findall(r"fetch\('py/([\w]+\.py)'\)", js)))
    assert fetched == _vendored_py_modules(), (
        "pyconsole.js fetch list differs from docs/console/py/: "
        f"fetched={fetched} on_disk={_vendored_py_modules()}"
    )


def test_pyconsole_writes_every_fetched_module():
    js = _read_pyconsole()
    fetched = set(re.findall(r"fetch\('py/([\w]+\.py)'\)", js))
    written = set(re.findall(r"FS\.writeFile\('/takenweb/([\w]+\.py)'", js))
    assert written == fetched, (
        f"pyconsole.js writes {sorted(written)} but fetches {sorted(fetched)}"
    )
