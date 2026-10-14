"""Make the HA-independent protocol package importable without Home Assistant."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "custom_components" / "vividmoo"))
