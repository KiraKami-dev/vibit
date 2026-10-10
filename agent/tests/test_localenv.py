"""Loading agent/.env for local runs: the format, that the environment wins, and that platform runs skip it."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import localenv  # noqa: E402

SAMPLE = """
# a comment
LLM_BACKEND=anthropic
export LLM_MODEL="claude-haiku-5-5"
ANTHROPIC_API_KEY='sk-ant-placeholder'
LLM_EFFORT=low  # trailing comment
EMPTY=
not a line
1BAD=x
"""


class Parse(unittest.TestCase):
    def test_format(self):
        self.assertEqual(localenv.parse(SAMPLE), {
            "LLM_BACKEND": "anthropic", "LLM_MODEL": "claude-haiku-5-5", "ANTHROPIC_API_KEY": "sk-ant-placeholder",
            "LLM_EFFORT": "low", "EMPTY": ""})


class Load(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / ".env"
        self.path.write_text(SAMPLE, encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def test_environment_wins_and_empty_values_are_skipped(self):
        environ = {"LLM_BACKEND": "qwen", "LLM_EFFORT": ""}
        loaded = localenv.load(self.path, environ)
        self.assertEqual(environ["LLM_BACKEND"], "qwen")
        self.assertEqual(environ["LLM_EFFORT"], "")
        self.assertEqual(environ["LLM_MODEL"], "claude-haiku-5-5")
        self.assertNotIn("EMPTY", environ)
        self.assertEqual(sorted(loaded), ["ANTHROPIC_API_KEY", "LLM_MODEL"])

    def test_missing_file_is_fine(self):
        environ: dict[str, str] = {}
        self.assertEqual(localenv.load(Path(self.dir.name) / "missing.env", environ), [])
        self.assertEqual(environ, {})

    def test_platform_runs_skip_the_file(self):
        environ = {"CHALLENGE_RUN_TOKEN": "rt_example"}
        self.assertEqual(localenv.load(self.path, environ), [])
        self.assertNotIn("LLM_BACKEND", environ)


if __name__ == "__main__":
    unittest.main()
