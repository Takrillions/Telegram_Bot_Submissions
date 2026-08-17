import unittest
from authorization import (
    GlobalAction,
    GlobalAuthorizer,
    GlobalRole,
    is_superadmin,
    parse_superadmin_telegram_id,
)


class GlobalAuthorizationTests(unittest.TestCase):
    def test_superadmin_match_is_exact_and_fail_closed(self):
        self.assertTrue(is_superadmin(100, 100))
        self.assertFalse(is_superadmin(101, 100))
        self.assertFalse(is_superadmin(None, 100))
        self.assertFalse(is_superadmin(100, None))
        self.assertFalse(is_superadmin(100, 0))

    def test_global_authorizer_allows_only_configured_superadmin(self):
        auth = GlobalAuthorizer(superadmin_telegram_id=100)
        allowed = auth.require(actor_id=100, action=GlobalAction.PRESTART_PROFILE)
        denied = auth.require(actor_id=101, action=GlobalAction.PRESTART_PROFILE)
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.role, GlobalRole.SUPERADMIN)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "superadmin_required")

    def test_missing_or_invalid_configuration_disables_global_access(self):
        for configured in (None, 0, -1):
            with self.subTest(configured=configured):
                result = GlobalAuthorizer(superadmin_telegram_id=configured).require(
                    actor_id=100, action=GlobalAction.PRESTART_PROFILE
                )
                self.assertFalse(result.allowed)
                self.assertEqual(result.reason, "superadmin_not_configured")


class SuperadminConfigurationTests(unittest.TestCase):
    def test_positive_numeric_id_is_loaded(self):
        self.assertEqual(parse_superadmin_telegram_id("123456789"), 123456789)
        self.assertEqual(parse_superadmin_telegram_id(" 123456789 "), 123456789)

    def test_missing_or_invalid_id_fails_closed(self):
        for value in (None, "", "abc", "0", "-5", "1.5"):
            with self.subTest(value=value):
                self.assertIsNone(parse_superadmin_telegram_id(value))


if __name__ == "__main__":
    unittest.main()
