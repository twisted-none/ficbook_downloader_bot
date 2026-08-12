from __future__ import annotations

import unittest

from src.bot.ui import WELCOME_TEXT, active_download_status, duration_text, main_keyboard, queue_status, sending_status


class BotUiTests(unittest.TestCase):
    def test_admin_commands_are_only_on_admin_keyboard(self) -> None:
        user_buttons = {button.text for row in main_keyboard().keyboard for button in row}
        admin_buttons = {button.text for row in main_keyboard(is_admin=True).keyboard for button in row}

        self.assertNotIn("/broadcast", user_buttons)
        self.assertNotIn("/queue", user_buttons)
        self.assertIn("/broadcast", admin_buttons)
        self.assertIn("/queue", admin_buttons)

    def test_welcome_mentions_ao3_and_free_litnet_books(self) -> None:
        self.assertIn("Ficbook, AO3, Wattpad, Hogwartsnet", WELCOME_TEXT)
        self.assertIn("бесплатные книги Litnet", WELCOME_TEXT)
        self.assertIn("Платные книги Litnet бот не скачивает", WELCOME_TEXT)

    def test_queue_status_shows_position_without_total(self) -> None:
        text = queue_status(("fb2", "epub"), 3, 429 * 60)

        self.assertIn("Место в очереди: 3.", text)
        self.assertNotIn("3/", text)
        self.assertIn("Примерное ожидание: 7 часов 9 минут.", text)
        self.assertIn("\n\nФорматы: FB2, EPUB.", text)

    def test_queue_status_explains_ficbook_outage(self) -> None:
        text = queue_status(("fb2",), 3, 240, ficbook_unavailable=True)

        self.assertIn("Место в очереди: 3.", text)
        self.assertIn("до восстановления Ficbook", text)
        self.assertIn("Фанфик скачается, как только сайт снова заработает", text)

    def test_duration_uses_russian_plural_forms(self) -> None:
        cases = {
            60: "1 минута",
            120: "2 минуты",
            300: "5 минут",
            660: "11 минут",
            1260: "21 минута",
            3600: "1 час",
            7320: "2 часа 2 минуты",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(duration_text(seconds), expected)

    def test_active_and_sending_status_use_five_circles(self) -> None:
        active = active_download_status(("pdf",), 40)
        sending = sending_status(("pdf",))

        self.assertEqual(active.count("🟢"), 2)
        self.assertIn("Ваш фанфик скачивается...", active)
        self.assertEqual(sending.count("🟢"), 5)
        self.assertIn("100%", sending)
        self.assertIn("Отправляем...", sending)


if __name__ == "__main__":
    unittest.main()
