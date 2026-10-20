"""CLI subcommand tests: argument parsing, exit codes, and JSON output."""

from __future__ import annotations

import json
import unittest
from io import StringIO
from typing import Mapping, Optional

from domainkit.cli import main


class CliTests(unittest.TestCase):
    def _run(self, argv: list[str], env: Optional[Mapping[str, str]] = None) -> tuple[int, str]:
        out = StringIO()
        exit_code = main(argv, out=out, env=env or {})
        return exit_code, out.getvalue()

    def test_names_prints_branch_and_variable(self) -> None:
        code, output = self._run(["names", "livingtechnology.com"])
        self.assertEqual(code, 0)
        self.assertIn("livingtechnology.com", output)
        self.assertIn("CLOUDFRONT_LIVINGTECHNOLOGY_COM", output)

    def test_names_accepts_custom_prefix(self) -> None:
        code, output = self._run(["names", "livingtechnology.com", "--prefix", "CDN"])
        self.assertEqual(code, 0)
        self.assertIn("CDN_LIVINGTECHNOLOGY_COM", output)

    def test_names_works_without_credentials(self) -> None:
        code, output = self._run(["names", "livingtechnology.com"], env={})
        self.assertEqual(code, 0)
        self.assertIn("livingtechnology.com", output)

    def test_plan_without_credentials_reports_missing_config(self) -> None:
        code, output = self._run(["plan", "livingtechnology.com"], env={})
        self.assertNotEqual(code, 0)
        self.assertTrue("AWS_ACCESS_KEY_ID" in output or "credentials" in output.lower())

    def test_apply_without_yes_returns_exit_code_2(self) -> None:
        code, _output = self._run(["apply", "livingtechnology.com"], env={})
        self.assertEqual(code, 2)

    def test_apply_with_yes_but_no_credentials_fails(self) -> None:
        code, output = self._run(["apply", "--yes", "livingtechnology.com"], env={})
        self.assertNotEqual(code, 0)
        self.assertNotEqual(code, 2)
        self.assertTrue("AWS_ACCESS_KEY_ID" in output or "credentials" in output.lower())

    def test_json_flag_emits_parseable_output(self) -> None:
        # --json is a global flag, so it precedes the subcommand.
        code, output = self._run(["--json", "names", "livingtechnology.com"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIsInstance(payload, list)
        self.assertTrue(any(item["branch"] == "livingtechnology.com" for item in payload))

    def test_doctor_without_credentials_reports_missing_config(self) -> None:
        code, output = self._run(["doctor"], env={})
        self.assertNotEqual(code, 0)
        self.assertTrue("AWS_ACCESS_KEY_ID" in output or "credentials" in output.lower())

    def test_variables_without_credentials_fails(self) -> None:
        code, output = self._run(["variables"], env={})
        self.assertNotEqual(code, 0)
        self.assertTrue("GITLAB" in output or "credentials" in output.lower())

    def test_doctor_reports_the_gitlab_instance_it_targets(self) -> None:
        _code, output = self._run(
            ["doctor"],
            env={
                "GITLAB_URL": "https://doc-git.example.com",
                "GITLAB_PROJECT_ID": "Ruby / Ruby-WebSite",
                "GITLAB_TOKEN": "glpat-token",
            },
        )
        self.assertIn("gitlab-target", output)
        self.assertIn("https://doc-git.example.com", output)
        self.assertIn("Ruby/Ruby-WebSite", output)
        self.assertNotIn("-> project Ruby / Ruby-WebSite", output)

    def test_doctor_blank_gitlab_url_is_reported_as_public_gitlab(self) -> None:
        _code, output = self._run(
            ["doctor"],
            env={"GITLAB_URL": "", "GITLAB_PROJECT_ID": "group/repo", "GITLAB_TOKEN": "glpat-token"},
        )
        self.assertIn("https://gitlab.com -> project group/repo", output)


if __name__ == "__main__":
    unittest.main()
