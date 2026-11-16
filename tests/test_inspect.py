"""Run with: python -m unittest discover -s tests"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harbor_inspect as hi  # noqa: E402


def make_task(root: Path, toml: str, *, test_sh: str = "echo 1 > /logs/verifier/reward.txt\n",
              dockerfile: str = "FROM ubuntu:24.04\n", solution: bool = True, tests_dockerfile: bool = False) -> Path:
    for sub in ("environment", "tests", "solution"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "task.toml").write_text(toml)
    (root / "instruction.md").write_text("<!-- canary GUID 123 -->\nWrite the answer to /app/out.txt.\n")
    (root / "environment" / "Dockerfile").write_text(dockerfile)
    (root / "tests" / "test.sh").write_text(test_sh)
    if tests_dockerfile:
        (root / "tests" / "Dockerfile").write_text("FROM ubuntu:24.04\nCOPY . /tests/\n")
    if solution:
        (root / "solution" / "solve.sh").write_text("echo hi > /app/out.txt\n")
    return root


class ReadTests(unittest.TestCase):
    def test_verifier_mode_resolution(self):
        cases = {
            '[verifier]\ntimeout_sec = 60\n': "shared",
            '[verifier]\nenvironment_mode = "separate"\n': "separate",
            '[verifier.environment]\ncpus = 2\n': "separate",
            '[verifier]\nenvironment_mode = "shared"\n[verifier.environment]\ncpus = 2\n': "invalid",
        }
        for toml, want in cases.items():
            with tempfile.TemporaryDirectory() as tmp:
                t = hi.read_task(make_task(Path(tmp), 'schema_version = "1.4"\n' + toml))
                self.assertEqual(t["verifier_mode"], want, toml)

    def test_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = make_task(Path(tmp) / "clean", 'schema_version = "1.4"\nartifacts = ["/app/out.txt"]\n'
                              '[agent]\ntimeout_sec = 60\nuser = "agent"\n[verifier]\nenvironment_mode = "separate"\n',
                              dockerfile="FROM ubuntu@sha256:" + "a" * 64 + "\n", tests_dockerfile=True)
            self.assertEqual(hi.flags_for(hi.read_task(clean)), [])
            messy = make_task(Path(tmp) / "messy", '[verifier.env]\nOPENAI_API_KEY = "${OPENAI_API_KEY}"\n',
                              test_sh="pip install pytest\npytest /tests\n", dockerfile="FROM someorg/base:latest\n",
                              solution=False)
            (messy / "environment" / "expected_answers.json").write_text("{}")
            got = set(hi.flags_for(hi.read_task(messy)))
            self.assertTrue({"shared-verifier", "verifier-installs-at-test-time", "verifier-needs-secret",
                             "unpinned-image", "image-not-in-task", "no-solution", "no-reward-write",
                             "no-agent-timeout", "root-agent", "answer-like-file-in-environment"} <= got, got)

    def test_census_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(3):
                make_task(Path(tmp) / f"t{i}", 'schema_version = "1.4"\n[agent]\ntimeout_sec = 120\n')
            s = hi.summarize(hi.load([tmp]))
            self.assertEqual(s["tasks"], 3)
            self.assertEqual(s["verifier_mode"], {"shared": 3})
            self.assertEqual(s["median_agent_timeout_sec"], 120)


class CollisionTests(unittest.TestCase):
    SEPARATE = 'schema_version = "1.4"\n[agent]\ntimeout_sec = 60\nuser = "agent"\n[verifier]\nenvironment_mode = "separate"\n'

    def _dataset(self, root: Path, names: tuple[str, str]) -> list[dict]:
        for task, key_name, key in (("a", names[0], '{"k": "AAAA"}'), ("b", names[1], '{"k": "BBBB"}')):
            d = make_task(root / task, self.SEPARATE, tests_dockerfile=True)
            (d / "tests" / key_name).write_text(key)
        return hi.load([str(root)])

    def test_same_path_and_size_with_different_content_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = self._dataset(Path(tmp), ("gold.json", "gold.json"))
            for t in tasks:
                self.assertIn("build-context-collision", hi.flags_for(t))
                self.assertEqual(t["context_collisions"], ["tests/gold.json"])

    def test_distinct_names_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            for t in self._dataset(Path(tmp), ("gold.aaaa.json", "gold.bbbb.json")):
                self.assertNotIn("build-context-collision", hi.flags_for(t))

    def test_identical_dockerfiles_and_scripts_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            for t in self._dataset(Path(tmp), ("gold.aaaa.json", "gold.bbbb.json")):
                self.assertEqual(t["context_collisions"], [])


if __name__ == "__main__":
    unittest.main()
