from __future__ import annotations

import unittest

from src.bot.app import _set_bot_commands


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[list[object], object | None]] = []

    async def set_my_commands(self, commands: list[object], scope: object | None = None) -> None:
        self.calls.append((commands, scope))


class AdminCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_restricted_commands_are_only_in_admin_scope(self) -> None:
        bot = FakeBot()

        await _set_bot_commands(bot, 123)  # type: ignore[arg-type]

        public = {command.command for command in bot.calls[0][0]}
        admin = {command.command for command in bot.calls[1][0]}
        self.assertNotIn("broadcast", public)
        self.assertNotIn("queue", public)
        self.assertIn("broadcast", admin)
        self.assertIn("queue", admin)
        self.assertEqual(bot.calls[1][1].chat_id, 123)


if __name__ == "__main__":
    unittest.main()
