# conftest.py – add backend/ to sys.path so all imports work from tests/
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
