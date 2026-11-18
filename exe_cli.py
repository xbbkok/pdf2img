#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行版 exe 的入口（PyInstaller 打包用）。"""

import multiprocessing
import sys

from pdf2img import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
