"""Documentation must not hand operators unsafe commands or promises the code does not keep."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class DockerGuideTests(unittest.TestCase):
    def test_volume_backups_are_private_and_outside_the_repository(self):
        # The archive holds every transcript, gateway.db and Aelix's auth.json.
        commands = [x for x in re.findall(r"docker run .*?(?:\n(?!\S).*?)*$", read("docs/docker.md"), re.M)
                    if re.search(r"\btar\s", x)]
        self.assertEqual(len(commands), 2)  # backup and restore
        for command in commands:
            with self.subTest(command=command.split("\n")[0]):
                self.assertRegex(command, r"umask 077 && tar ")
                self.assertNotIn("$PWD", command)

    def test_local_deployment_files_are_ignored_by_git(self):
        if shutil.which("git") is None or not (ROOT / ".git").exists():
            self.skipTest("needs a git checkout")

        def ignored(path: str) -> bool:
            return subprocess.run(["git", "check-ignore", "--no-index", "-q", path], cwd=ROOT).returncode == 0

        for path in ("deploy/docker/config.toml", "deploy/docker/provider.env", "deploy/docker/models.json",
                     "deploy/docker/secrets/mattermost_token", "deploy/docker/extensions/tools.py",
                     "deploy/docker/compose.override.yaml",
                     "deploy/docker/state-20261008.tgz"):
            self.assertTrue(ignored(path), path)
        for path in ("deploy/docker/config.toml.example", "deploy/docker/models.json.example",
                     "deploy/docker/provider.env.example", "deploy/docker/compose.yaml"):
            self.assertFalse(ignored(path), path)


class ClaimTests(unittest.TestCase):
    def test_mention_neutralization_is_described_with_its_limit(self):
        # Members' own keywords and first names notify without an "@" (mention_keywords.go).
        for name, limit in (("README.md", "keywords"), ("SECURITY.md", "keywords"), ("README.ko.md", "키워드")):
            with self.subTest(document=name):
                text = read(name)
                self.assertNotRegex(text, r"(?i)mentions in model output notify nobody\.|cannot notify people")
                self.assertIn(limit, text)

    def test_unported_markdown_is_described_with_its_scope_and_limit(self):
        # mentions.neutralize_mentions turns vertical tabs and form feeds into spaces, even in code,
        # and an uncertain reference label (cased letters only) makes only its own paragraph plain
        # text (pinned in test_mattermost); the port is checked against the server, not proven.
        text = " ".join(read("SECURITY.md").split())
        self.assertIn("Vertical tabs and form feeds become spaces first, also in code", text)
        self.assertNotIn("form feed anywhere in a post: every paragraph", text)
        self.assertIn("letters with upper and lower case", text)
        self.assertIn("that paragraph only", text)
        self.assertIn("which is not a proof", text)
        self.assertIn("tested, not proven", " ".join(read("README.md").split()))

    def test_doctor_promises_no_credential_check(self):
        # Aelix's RPC cannot list available models, and doctor never makes a model request.
        text = read("README.md")
        self.assertNotIn("does not list the model as available", text)
        self.assertIn("cannot test provider credentials", text)


if __name__ == "__main__":
    unittest.main()
