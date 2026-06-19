# -*- coding: utf-8 -*-
"""Пути для тестов: корень проекта и сама папка tests в sys.path."""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)

for path in (PROJECT_DIR, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)
