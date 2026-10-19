"""Keep JSII's runtime package cache inside the writable project workspace."""

import os
from pathlib import Path

os.environ.setdefault(
    "JSII_RUNTIME_PACKAGE_CACHE_ROOT",
    str(Path(__file__).resolve().parents[1] / ".cache" / "jsii"),
)
