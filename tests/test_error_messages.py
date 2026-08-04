from __future__ import annotations

from types import SimpleNamespace
import unittest

from src.bot.app import _notify_admin, _user_error_message
from src.sources.ficbook import FicbookAccount, FicbookClient, FicbookLoginError
from src.sources.litnet import LitnetLoginError


DETAIL = "Не удалось войти в Litnet. Проверь LITNET_LOGIN и LITNET_PASSWORD."
PUBLIC = "Авторизация через Litnet не сработала."


class AlertBot:
    def __init__(self) -> None:
        self.text = ""

    async def send_message(self, _: int, text: str) -> None:
        self.text = text


class ErrorMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_litnet_login_keeps_separate_admin_and_user_messages(self) -> None:
        client = FicbookClient()

        def fail(*_: object) -> None:
            raise LitnetLoginError(DETAIL)

        client.litnet.download = fail  # type: ignore[method-assign]
        with self.assertRaises(FicbookLoginError) as raised:
            client._download_once(
                "https://litnet.com/ru/reader/test-b1",
                FicbookAccount(),
                None,
                None,
            )

        self.assertEqual(str(raised.exception), DETAIL)
        self.assertEqual(_user_error_message(raised.exception), PUBLIC)

    def test_private_configuration_is_hidden_by_fallback(self) -> None:
        error = FicbookLoginError("Проверь SITE_LOGIN и SITE_PASSWORD в .env.")

        self.assertEqual(_user_error_message(error), "Авторизация на сайте не сработала.")

    async def test_alert_bot_still_receives_detailed_message(self) -> None:
        alert_bot = AlertBot()
        message = SimpleNamespace(
            from_user=SimpleNamespace(username="user", full_name="User", id=1),
            chat=SimpleNamespace(id=2),
            bot=alert_bot,
        )
        error = FicbookLoginError(DETAIL, user_message=PUBLIC)

        await _notify_admin(message, 3, alert_bot, "https://litnet.com/ru/book/test-b1", error, expected=True)

        self.assertIn(DETAIL, alert_bot.text)
        self.assertNotIn(PUBLIC, alert_bot.text)


if __name__ == "__main__":
    unittest.main()
