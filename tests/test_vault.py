"""Credential store tests: round-trip, masking, and blank-value semantics."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from domainkit.errors import ConfigError
from domainkit.vault import (
    CREDENTIAL_KEYS,
    apply_to_env,
    delete_credentials,
    describe,
    load_credentials,
    mask,
    save_credentials,
    store_path,
)


class VaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "credentials.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_round_trip_preserves_values(self) -> None:
        values = {
            "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "secret-value",
            "GITLAB_URL": "https://doc-git.example.com",
            "GITLAB_PROJECT_ID": "group/repo",
            "GITLAB_TOKEN": "glpat-token",
        }
        save_credentials(values, path=self.path)
        self.assertEqual(load_credentials(path=self.path), values)

    def test_spaceship_credentials_round_trip_mask_and_delete(self) -> None:
        values = {"SPACESHIP_API_KEY": "test-key-private", "SPACESHIP_API_SECRET": "test-secret-private",
                  "SPACESHIP_API_ENDPOINT": "https://spaceship.dev/api"}
        save_credentials(values, path=self.path)
        self.assertEqual(load_credentials(path=self.path), values)
        self.assertNotIn(values["SPACESHIP_API_SECRET"], self.path.read_text(encoding="utf-8"))
        self.assertNotIn(values["SPACESHIP_API_SECRET"], describe(values)["SPACESHIP_API_SECRET"])
        self.assertTrue(delete_credentials(path=self.path))

    def test_secrets_are_not_stored_in_clear_text(self) -> None:
        save_credentials({"GITLAB_TOKEN": "glpat-super-secret"}, path=self.path)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("glpat-super-secret", raw)
        self.assertIn("magic", json.loads(raw))

    def test_blank_values_are_not_persisted(self) -> None:
        save_credentials({"GITLAB_TOKEN": "glpat-token", "GODADDY_API_KEY": "   "}, path=self.path)
        stored = load_credentials(path=self.path)
        self.assertIn("GITLAB_TOKEN", stored)
        self.assertNotIn("GODADDY_API_KEY", stored)

    def test_missing_file_loads_as_empty(self) -> None:
        self.assertEqual(load_credentials(path=self.path), {})

    def test_foreign_file_is_rejected(self) -> None:
        self.path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_credentials(path=self.path)

    def test_delete_reports_whether_a_file_was_removed(self) -> None:
        self.assertFalse(delete_credentials(path=self.path))
        save_credentials({"GITLAB_TOKEN": "glpat-token"}, path=self.path)
        self.assertTrue(delete_credentials(path=self.path))
        self.assertFalse(self.path.exists())

    def test_apply_to_env_drops_blank_keys_instead_of_writing_empty(self) -> None:
        env = {"GITLAB_URL": "https://gitlab.com", "GODADDY_API_KEY": "stale"}
        apply_to_env(env, {"GITLAB_URL": "https://doc-git.example.com", "GODADDY_API_KEY": ""})
        self.assertEqual(env["GITLAB_URL"], "https://doc-git.example.com")
        self.assertNotIn("GODADDY_API_KEY", env)

    def test_apply_to_env_never_writes_empty_strings(self) -> None:
        env = {key: "value" for key in CREDENTIAL_KEYS}
        apply_to_env(env, {})
        self.assertEqual(env, {})

    def test_mask_hides_the_tail_and_shows_length(self) -> None:
        rendered = mask("glpat-abcdefghijklmnop")
        self.assertTrue(rendered.startswith("glpa"))
        self.assertNotIn("abcdefghijklmnop", rendered)
        self.assertIn("22", rendered)

    def test_mask_reports_unset_for_blank(self) -> None:
        self.assertEqual(mask(""), "(未设置)")
        self.assertEqual(mask("   "), "(未设置)")

    def test_describe_masks_secrets_but_shows_plain_fields(self) -> None:
        summary = describe(
            {
                "GITLAB_URL": "https://doc-git.example.com",
                "GITLAB_PROJECT_ID": "group/repo",
                "GITLAB_TOKEN": "glpat-abcdefghijk",
                "AWS_ACCESS_KEY_ID": "AKIAEXAMPLEKEYTAIL",
                "AWS_SECRET_ACCESS_KEY": "very-secret-value",
            }
        )
        self.assertEqual(summary["GITLAB_URL"], "https://doc-git.example.com")
        self.assertEqual(summary["GITLAB_PROJECT_ID"], "group/repo")
        self.assertNotIn("abcdefghijk", summary["GITLAB_TOKEN"])
        self.assertNotIn("very-secret-value", summary["AWS_SECRET_ACCESS_KEY"])
        self.assertNotIn("EXAMPLEKEYTAIL", summary["AWS_ACCESS_KEY_ID"])
        self.assertEqual(summary["GODADDY_API_KEY"], "(未设置)")

    def test_store_path_honours_explicit_override(self) -> None:
        custom = str(Path(self._dir.name) / "custom" / "creds.json")
        self.assertEqual(store_path({"DOMAINKIT_CREDENTIAL_STORE": custom}), Path(custom))

    def test_store_path_falls_back_to_localappdata(self) -> None:
        resolved = store_path({"LOCALAPPDATA": self._dir.name})
        self.assertEqual(resolved.parent.name, "domainkit")
        self.assertEqual(resolved.name, "credentials.json")


if __name__ == "__main__":
    unittest.main()
