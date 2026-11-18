#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形界面版 exe 的入口（PyInstaller 打包用）。"""

import multiprocessing
import sys

from pdf2img_gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
