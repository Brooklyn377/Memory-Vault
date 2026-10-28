"""Reads /etc/memory-agent/config.yaml (or the file named in MEMORY_AGENT_CONFIG). Keys: config.example.yaml."""
import os
from pathlib import Path

import yaml

PATH = Path(os.environ.get("MEMORY_AGENT_CONFIG", "/etc/memory-agent/config.yaml"))


def load():
    return yaml.safe_load(PATH.read_text()) or {}
