import unittest

from prestart_card import (
    BOTFATHER_URL,
    description_picture_apply_instructions,
    description_picture_remove_instructions,
)


class PreStartCardLogicTests(unittest.TestCase):
    def test_botfather_url_is_direct_and_https(self):
        self.assertEqual(BOTFATHER_URL, "https://t.me/BotFather")

    def test_apply_instructions_cover_all_supported_media(self):
        for media_type in ("photo", "video", "animation"):
            text = description_picture_apply_instructions(media_type)
            self.assertIn("@BotFather", text)
            self.assertIn("Edit Description Picture", text)
            self.assertNotIn("profile photo", text.lower())

    def test_apply_instructions_reject_unsupported_media(self):
        with self.assertRaises(ValueError):
            description_picture_apply_instructions("document")

    def test_remove_instructions_do_not_claim_remote_removal(self):
        text = description_picture_remove_instructions()
        self.assertIn("Локальная настройка медиа очищена", text)
        self.assertIn("@BotFather", text)
        self.assertIn("Edit Description Picture", text)


if __name__ == "__main__":
    unittest.main()
