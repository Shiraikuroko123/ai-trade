import json
import tempfile
import unittest
from pathlib import Path

from ai_trade.web.auth import (
    MIN_PBKDF2_ITERATIONS,
    PASSWORD_ALGORITHM,
    PASSWORD_HASH_VERSION,
    AuthManager,
    AuthenticationError,
    CorruptUserStoreError,
    InvalidUsernameError,
    LoginRateLimiter,
    PasswordPolicyError,
    SessionStore,
    UserAlreadyExistsError,
    UserStore,
    UserStoreError,
)


PASSWORD = "Correct horse battery staple!"
NEW_PASSWORD = "Another strong password value!"


class FakeClock:
    def __init__(self, value=1_700_000_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class WebAuthTests(unittest.TestCase):
    def test_user_management_normalizes_and_never_stores_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "users.json"
            store = UserStore(path, iterations=MIN_PBKDF2_ITERATIONS)
            self.assertFalse(store.has_users())

            user = store.add_user("  Ａlice.User  ", PASSWORD)
            self.assertEqual(user.username, "alice.user")
            self.assertTrue(user.enabled)
            self.assertTrue(store.has_users())
            self.assertTrue(store.verify("ALICE.USER", PASSWORD))
            self.assertFalse(store.verify("alice.user", "Incorrect password value!"))

            with self.assertRaises(UserAlreadyExistsError):
                store.add_user("Alice.User", NEW_PASSWORD)
            with self.assertRaises(InvalidUsernameError):
                store.add_user("not valid!", NEW_PASSWORD)
            store.add_user("shortpass", "12345678")
            self.assertTrue(store.verify("shortpass", "12345678"))
            with self.assertRaises(PasswordPolicyError):
                store.add_user("too-short", "1234567")

            payload = json.loads(path.read_text(encoding="utf-8"))
            password_record = payload["users"][0]["password"]
            self.assertEqual(password_record["version"], PASSWORD_HASH_VERSION)
            self.assertEqual(password_record["algorithm"], PASSWORD_ALGORITHM)
            self.assertNotIn(PASSWORD, path.read_text(encoding="utf-8"))
            self.assertFalse(any(Path(temporary).glob("*.tmp")))

            store.add_user("alice.user", NEW_PASSWORD, replace=True)
            self.assertFalse(store.verify("alice.user", PASSWORD))
            self.assertTrue(store.verify("alice.user", NEW_PASSWORD))
            store.set_enabled("alice.user", False)
            self.assertFalse(store.verify("alice.user", NEW_PASSWORD))
            self.assertFalse(store.list_users()[0].enabled)
            store.set_enabled("alice.user", True)
            self.assertTrue(store.remove_user("alice.user"))
            self.assertFalse(store.remove_user("alice.user"))
            self.assertTrue(store.remove_user("shortpass"))
            self.assertFalse(store.has_users())

    def test_corrupt_user_file_is_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "users.json"
            original = "{not valid json"
            path.write_text(original, encoding="utf-8")
            store = UserStore(path, iterations=MIN_PBKDF2_ITERATIONS)

            with self.assertRaises(CorruptUserStoreError):
                store.list_users()
            with self.assertRaises(CorruptUserStoreError):
                store.add_user("alice", PASSWORD)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

            path.write_text(
                json.dumps({"schema_version": True, "users": []}),
                encoding="utf-8",
            )
            with self.assertRaises(CorruptUserStoreError):
                store.list_users()

    def test_session_expiry_revocation_and_session_bound_csrf(self):
        clock = FakeClock()
        sessions = SessionStore(ttl_seconds=60, clock=clock)
        grant = sessions.create("Alice", "a" * 64)

        self.assertNotEqual(grant.token, grant.session.csrf_token)
        self.assertEqual(grant.session.credential_revision, "a" * 64)
        self.assertEqual(sessions.authenticate(grant.token), grant.session)
        self.assertTrue(sessions.verify_csrf(grant.token, grant.session.csrf_token))
        self.assertFalse(sessions.verify_csrf(grant.token, "wrong-csrf-token"))

        second = sessions.create("alice", "b" * 64)
        self.assertTrue(sessions.revoke(second.token))
        self.assertIsNone(sessions.authenticate(second.token))
        clock.advance(60)
        self.assertIsNone(sessions.authenticate(grant.token))
        self.assertFalse(sessions.verify_csrf(grant.token, grant.session.csrf_token))

    def test_login_lockout_is_generic_and_recovers_after_expiry(self):
        with tempfile.TemporaryDirectory() as temporary:
            clock = FakeClock()
            users = UserStore(
                Path(temporary) / "users.json",
                iterations=MIN_PBKDF2_ITERATIONS,
            )
            users.add_user("alice", PASSWORD)
            limiter = LoginRateLimiter(
                max_failures=2,
                window_seconds=60,
                lockout_seconds=30,
                clock=clock,
            )
            manager = AuthManager(
                users,
                sessions=SessionStore(ttl_seconds=60, clock=clock),
                limiter=limiter,
            )

            with self.assertRaises(AuthenticationError) as wrong:
                manager.login("alice", "Incorrect password value!", source="loopback")
            with self.assertRaises(AuthenticationError) as missing:
                manager.login("missing", "Incorrect password value!", source="loopback")
            self.assertEqual(str(wrong.exception), str(missing.exception))
            self.assertEqual(wrong.exception.retry_after, 0.0)
            self.assertEqual(missing.exception.retry_after, 0.0)

            with self.assertRaises(AuthenticationError) as threshold:
                manager.login("alice", "Still incorrect password!", source="loopback")
            self.assertGreater(threshold.exception.retry_after, 0.0)
            with self.assertRaises(AuthenticationError) as locked:
                manager.login("alice", PASSWORD, source="loopback")
            self.assertGreater(locked.exception.retry_after, 0.0)
            self.assertEqual(str(threshold.exception), str(locked.exception))

            clock.advance(31)
            grant = manager.login("alice", PASSWORD, source="loopback")
            self.assertEqual(grant.username, "alice")

    def test_credential_changes_revoke_sessions_and_tokens_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "users.json"
            users = UserStore(path, iterations=MIN_PBKDF2_ITERATIONS)
            users.add_user("alice", PASSWORD)
            manager = AuthManager(users)

            grant = manager.login("alice", PASSWORD, source="loopback")
            disk = path.read_text(encoding="utf-8")
            self.assertNotIn(PASSWORD, disk)
            self.assertNotIn(grant.token, disk)
            self.assertNotIn(grant.session.csrf_token, disk)
            self.assertNotIn(grant.session.credential_revision, disk)
            self.assertEqual([item.name for item in root.iterdir()], ["users.json"])
            self.assertIsNotNone(manager.authenticate_session(grant.token))

            external_users = UserStore(path, iterations=MIN_PBKDF2_ITERATIONS)
            external_users.add_user("alice", NEW_PASSWORD, replace=True)
            self.assertIsNone(manager.authenticate_session(grant.token))
            with self.assertRaises(AuthenticationError):
                manager.login("alice", PASSWORD, source="loopback")

            replacement = manager.login("alice", NEW_PASSWORD, source="loopback")
            manager.disable_user("alice")
            self.assertIsNone(manager.authenticate_session(replacement.token))
            with self.assertRaises(AuthenticationError):
                manager.login("alice", NEW_PASSWORD, source="loopback")

    def test_portable_export_import_reject_replace_and_merge_modes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = UserStore(
                root / "source.json", iterations=MIN_PBKDF2_ITERATIONS
            )
            source.add_user("alice", PASSWORD)
            source.add_user("bob", NEW_PASSWORD)
            source.set_enabled("alice", False)
            portable = root / "portable-users.json"
            self.assertEqual(source.export_users(portable), 2)

            exported = json.loads(portable.read_text(encoding="utf-8"))
            self.assertEqual(set(exported), {"format", "version", "users"})
            self.assertNotIn(PASSWORD, portable.read_text(encoding="utf-8"))
            self.assertNotIn(NEW_PASSWORD, portable.read_text(encoding="utf-8"))
            self.assertNotIn("session", portable.read_text(encoding="utf-8").lower())

            target = UserStore(
                root / "target.json", iterations=MIN_PBKDF2_ITERATIONS
            )
            imported = target.import_users(portable)
            self.assertEqual([value.username for value in imported], ["alice", "bob"])
            self.assertFalse(target.verify("alice", PASSWORD))
            self.assertTrue(target.verify("bob", NEW_PASSWORD))
            with self.assertRaises(UserStoreError):
                target.import_users(portable)
            with self.assertRaises(UserAlreadyExistsError):
                target.import_users(portable, mode="merge")

            other = UserStore(root / "other.json", iterations=MIN_PBKDF2_ITERATIONS)
            other.add_user("carol", "Carol has a strong password!")
            other_export = root / "other-export.json"
            other.export_users(other_export)
            merged = target.import_users(other_export, mode="merge")
            self.assertEqual(
                [value.username for value in merged], ["alice", "bob", "carol"]
            )
            replaced = target.import_users(other_export, mode="replace")
            self.assertEqual([value.username for value in replaced], ["carol"])

    def test_import_rejects_tampering_bad_schema_and_duplicate_users_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = UserStore(
                root / "source.json", iterations=MIN_PBKDF2_ITERATIONS
            )
            source.add_user("alice", PASSWORD)
            portable = root / "portable-users.json"
            source.export_users(portable)
            valid = json.loads(portable.read_text(encoding="utf-8"))

            target = UserStore(
                root / "target.json", iterations=MIN_PBKDF2_ITERATIONS
            )
            target.add_user("keeper", NEW_PASSWORD)
            original_target = (root / "target.json").read_bytes()

            tampered = json.loads(json.dumps(valid))
            tampered["users"][0]["password"]["algorithm"] = "plaintext"
            portable.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(CorruptUserStoreError):
                target.import_users(portable, mode="replace")
            self.assertEqual((root / "target.json").read_bytes(), original_target)

            bad_base64 = json.loads(json.dumps(valid))
            bad_base64["users"][0]["password"]["digest"] = "not-base64!"
            portable.write_text(json.dumps(bad_base64), encoding="utf-8")
            with self.assertRaises(CorruptUserStoreError):
                target.import_users(portable, mode="replace")

            weak_iterations = json.loads(json.dumps(valid))
            weak_iterations["users"][0]["password"]["iterations"] = 1
            portable.write_text(json.dumps(weak_iterations), encoding="utf-8")
            with self.assertRaises(CorruptUserStoreError):
                target.import_users(portable, mode="replace")

            portable.write_text(
                json.dumps({"format": "ai-trade-users", "version": 99, "users": []}),
                encoding="utf-8",
            )
            with self.assertRaises(CorruptUserStoreError):
                target.import_users(portable, mode="replace")

            duplicated = json.loads(json.dumps(valid))
            duplicated["users"].append(duplicated["users"][0])
            portable.write_text(json.dumps(duplicated), encoding="utf-8")
            with self.assertRaises(CorruptUserStoreError):
                target.import_users(portable, mode="replace")
            self.assertEqual((root / "target.json").read_bytes(), original_target)


if __name__ == "__main__":
    unittest.main()
