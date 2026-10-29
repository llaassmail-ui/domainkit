import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from domainkit import vault
from domainkit.errors import ConfigError


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'credentials.json'

    def test_accounts_and_legacy_save_isolation(self):
        store = vault.empty_profiles()
        first = vault.put_profile(store, 'aws', 'first', {'AWS_SESSION_TOKEN': 'temporary'})
        vault.put_profile(store, 'aws', 'second', {'AWS_ACCESS_KEY_ID': 'second'})
        vault.save_profiles(store, path=self.path)
        self.assertNotIn('AWS_SESSION_TOKEN', vault.load_credentials(path=self.path))
        store['selected']['aws'] = first
        vault.save_profiles(store, path=self.path)
        vault.save_credentials({'GITLAB_PROJECT_ID': '43'}, path=self.path)
        updated = vault.load_profiles(path=self.path)
        self.assertEqual(len(updated['profiles']['aws']), 2)
        self.assertEqual(vault.selected_values(updated)['AWS_SESSION_TOKEN'], 'temporary')

    def test_blank_legacy_save_does_not_clear_existing_credentials(self):
        vault.save_credentials({'AWS_ACCESS_KEY_ID': 'example-key',
                                'AWS_SECRET_ACCESS_KEY': 'example-secret',
                                'AWS_SESSION_TOKEN': 'temporary'}, path=self.path)
        vault.save_credentials({'AWS_SESSION_TOKEN': ''}, path=self.path)
        self.assertEqual(vault.load_credentials(path=self.path)['AWS_SESSION_TOKEN'], 'temporary')

    def test_categories_allow_same_name_and_keep_selection_independent(self):
        store = vault.empty_profiles()
        aws_id = vault.put_profile(store, 'aws', 'production', {'AWS_ACCESS_KEY_ID': 'aws-key'})
        gl_id = vault.put_profile(store, 'gitlab', 'production', {'GITLAB_TOKEN': 'git-token',
                                                                  'GITLAB_PROJECT_ID': 'group/repo'})
        vault.save_profiles(store, path=self.path)
        loaded = vault.load_profiles(path=self.path)
        self.assertEqual(loaded['selected']['aws'], aws_id)
        self.assertEqual(loaded['selected']['gitlab'], gl_id)
        self.assertEqual(vault.load_credentials(path=self.path)['GITLAB_PROJECT_ID'], 'group/repo')

    def test_wrong_category_keys_are_rejected(self):
        store = vault.empty_profiles()
        store['profiles']['aws'].append({'id': 'one', 'name': 'bad',
                                         'values': {'GITLAB_TOKEN': 'example'}})
        with self.assertRaises(ConfigError):
            vault.save_profiles(store, path=self.path)

    def test_rename_duplicate_and_delete(self):
        store = vault.empty_profiles()
        first = vault.put_profile(store, 'aws', 'first', {})
        with self.assertRaises(ConfigError):
            vault.put_profile(store, 'aws', ' FIRST ', {})
        with self.assertRaises(ConfigError):
            vault.put_profile(store, 'aws', ' ', {})
        vault.put_profile(store, 'aws', 'renamed', {}, profile_id=first)
        vault.remove_profile(store, 'aws', first)
        self.assertIsNone(store['selected']['aws'])

    def test_migration_backup(self):
        envelope = {'magic': vault._MAGIC}
        envelope.update(vault._encrypt(json.dumps({'GITLAB_TOKEN': 'example-token'}).encode()))
        self.path.write_text(json.dumps(envelope), encoding='utf-8')
        original = self.path.read_bytes()
        store = vault.load_profiles(path=self.path)
        self.assertEqual(self.path.read_bytes(), original)
        vault.save_profiles(store, path=self.path)
        self.assertEqual(self.path.with_name('credentials.json.v1.bak').read_bytes(), original)

    def test_atomic_failure(self):
        store = vault.empty_profiles()
        vault.save_profiles(store, path=self.path)
        original = self.path.read_bytes()
        with patch.object(vault.os, 'replace', side_effect=OSError('failure')):
            with self.assertRaises(OSError):
                vault.save_profiles(store, path=self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_corrupt_file_preserved(self):
        self.path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ConfigError):
            vault.save_profiles(vault.empty_profiles(), path=self.path)
        self.assertEqual(self.path.read_text(), '{broken')

    def test_switch_clears_old_token(self):
        env = {'AWS_SESSION_TOKEN': 'old', 'GITLAB_TOKEN': 'git'}
        vault.apply_to_env(env, {'AWS_ACCESS_KEY_ID': 'new'}, vault.PROFILE_KEYS['aws'])
        self.assertNotIn('AWS_SESSION_TOKEN', env)
        self.assertEqual(env['GITLAB_TOKEN'], 'git')
