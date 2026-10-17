#!/usr/bin/env python3
"""Start the local task center from any skill installation path."""

import importlib.util
from pathlib import Path

script = Path(__file__).with_name("download_douyin.py")
spec = importlib.util.spec_from_file_location("douyin_download", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.configure_utf8_output()
module.ensure_dashboard()
print("http://127.0.0.1:9180")
