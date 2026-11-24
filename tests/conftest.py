"""Tests never touch the user's own settings: the config directory is a
temporary one for the whole run (tests that set their own still can)."""
import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="otterbench-tests-")
