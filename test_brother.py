import os
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import brother


def setUpModule():
    # Synthetic owner keeps tests independent from private local configuration.
    global owner_patch
    owner_patch = patch.object(brother, "OWNER_ID", 100)
    owner_patch.start()


def tearDownModule():
    owner_patch.stop()


def fake_update(text="", user_id=200):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(message=message, effective_message=message, effective_user=user)


class TemporaryMemory:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.memory_path = Path(temporary.name) / "memory.json"
        memory_patch = patch.object(brother, "MEMORY_FILE", self.memory_path)
        memory_patch.start()
        self.addCleanup(memory_patch.stop)


class LocalResponseTests(unittest.TestCase):
    def test_short_local_replies(self):
        cases = {
            "привет": "Привет.",
            "йо брат": "Йо, брат.",
            "ку!": "Привет, брат.",
            "йо, брат": "Привет, брат.",
            "hello!": "Hi, bro.",
            "hi": "Hi, bro.",
            "здарова": "Привет, брат.",
            "как дела?": "Работаю, брат. Мозги уже завезли 😎",
            "как оно": "Работаю, брат. Мозги уже завезли 😎",
            "брат?!": "Я тут, брат.",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(brother.get_response(text), expected)

    def test_substrings_and_substantive_questions_go_to_ai(self):
        for text in ("покупка", "покупка квартиры", "machine", "братство", "расскажи о python, брат", "что такое python"):
            with self.subTest(text=text):
                self.assertIsNone(brother.get_response(text))


class GreetingRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_greeting_with_question_reaches_ai_unchanged(self):
        for text in (
            "Hi! What can you help me with? Keep it brief.",
            "Привет! Что ты умеешь?",
            "Как дела? Объясни Python.",
        ):
            with self.subTest(text=text), patch("brother.ask_ai", new_callable=AsyncMock, return_value="AI_REPLY") as ai:
                update = fake_update(text)
                await brother.handle_message(update, None)
                ai.assert_awaited_once_with(text, 200)
                update.message.reply_text.assert_awaited_once_with("AI_REPLY")


class AIFallbackTests(TemporaryMemory, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        # Все запросы проходят через подменённый клиент; настоящая сеть не используется.
        self.env_patch = patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-placeholder"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.factory_patch = patch("brother.httpx.AsyncClient")
        self.factory = self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)
        self.client = AsyncMock()
        self.factory.return_value.__aenter__.return_value = self.client
        self.request = httpx.Request("POST", "https://example.invalid/test?secret=PRIVATE_TEST_MARKER")

    def response(self, code=200, **kwargs):
        return httpx.Response(code, request=self.request, **kwargs)

    async def test_missing_or_blank_key_never_creates_client(self):
        for value in (None, "", "   "):
            with self.subTest(value=value), patch("brother.os.getenv", return_value=value):
                reply = await brother.ask_ai("Python", 200)
                self.assertIn("не настроен", reply)
        self.factory.assert_not_called()

    async def test_successful_ai_reply_preserves_original_request(self):
        text = "  Расскажи о Python, брат  "
        answer = "Python — язык программирования."
        self.client.post.return_value = self.response(json={"choices": [{"message": {"content": answer}}]})
        update = fake_update(text)
        await brother.handle_message(update, None)
        update.message.reply_text.assert_awaited_once_with(answer)
        self.client.post.assert_awaited_once()
        payload = self.client.post.call_args.kwargs["json"]
        self.assertEqual(payload["messages"][-1], {"role": "user", "content": text})
        self.assertEqual(payload["model"], "openrouter/free")

    async def test_local_handler_normalizes_without_calling_ai(self):
        update = fake_update("  ПРИВЕТ  ")
        await brother.handle_message(update, None)
        update.message.reply_text.assert_awaited_once_with("Привет.")
        self.factory.assert_not_called()

    async def test_http_errors_return_safe_useful_messages(self):
        cases = (
            (400, "формата"), (422, "формата"), (402, "балансом"),
            (401, "авторизацию"), (403, "авторизацию"), (404, "недоступны"),
            (408, "не ответил вовремя"), (413, "слишком большой"),
            (429, "лимит"), (500, "недоступен"), (503, "недоступен"),
        )
        for code, expected in cases:
            with self.subTest(code=code):
                self.client.post.return_value = self.response(
                    code,
                    json={"error": {"message": "PRIVATE_TEST_MARKER", "notes": "PRIVATE_NOTES_MARKER", "prompt": "PRIVATE_PROMPT_MARKER"}},
                    headers={"X-Test-Marker": "PRIVATE_HEADER_MARKER"},
                )
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    reply = await brother.ask_ai("Python", 200)
                self.assertIn(expected, reply)
                self.assertTrue(reply.startswith(f"HTTP {code}: "))
                combined = reply + stdout.getvalue() + stderr.getvalue()
                for private in ("PRIVATE_TEST_MARKER", "PRIVATE_NOTES_MARKER", "PRIVATE_PROMPT_MARKER", "PRIVATE_HEADER_MARKER", "test-placeholder", "https://"):
                    self.assertNotIn(private, combined)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    async def test_other_http_codes_keep_numeric_code_and_safe_fallback(self):
        for code in (302, 409, 418):
            with self.subTest(code=code):
                self.client.post.return_value = self.response(code, json={"error": "PRIVATE_TEST_MARKER"})
                reply = await brother.ask_ai("Python", 200)
                self.assertEqual(reply, f"HTTP {code}: AI отклонил запрос, брат. Попробуй позже.")

    async def test_network_and_timeout_errors_do_not_leak_exception_text(self):
        for error_type, expected in ((httpx.ReadTimeout, "не ответил вовремя"), (httpx.ConnectTimeout, "не ответил вовремя"), (httpx.ConnectError, "подключиться"), (httpx.RemoteProtocolError, "подключиться")):
            with self.subTest(error=error_type.__name__):
                self.client.post.side_effect = error_type("PRIVATE_TEST_MARKER", request=self.request)
                reply = await brother.ask_ai("Python", 200)
                self.assertIn(expected, reply)
                self.assertNotIn("PRIVATE_TEST_MARKER", reply)
                self.assertNotIn("https://", reply)

    async def test_malformed_json_returns_safe_message(self):
        self.client.post.return_value = self.response(content=b"PRIVATE_TEST_MARKER is not JSON")
        reply = await brother.ask_ai("Python", 200)
        self.assertIn("некорректный ответ", reply)
        self.assertNotIn("PRIVATE_TEST_MARKER", reply)

    async def test_invalid_response_shapes_and_empty_content(self):
        invalid_results = [
            None, [], {}, {"choices": []}, {"choices": {}},
            {"choices": [None]}, {"choices": [{}]},
            {"choices": [{"message": None}]},
            {"choices": [{"message": []}]},
            {"choices": [{"message": {}}]},
            *[{"choices": [{"message": {"content": content}}]} for content in (None, "", "   ", [], {}, 123, True)],
        ]
        for payload in invalid_results:
            with self.subTest(payload=payload):
                self.client.post.return_value = self.response(json=payload)
                reply = await brother.ask_ai("Python", 200)
                self.assertIn("некорректный ответ", reply)

    async def test_provider_failure_is_sent_back_to_telegram_user(self):
        self.client.post.return_value = self.response(503, json={"error": "PRIVATE_TEST_MARKER"})
        update = fake_update("Объясни Python")
        await brother.handle_message(update, None)
        update.message.reply_text.assert_awaited_once()
        reply = update.message.reply_text.call_args.args[0]
        self.assertIn("недоступен", reply)
        self.assertNotIn("PRIVATE_TEST_MARKER", reply)


    async def test_payload_isolates_a_b_and_owner(self):
        memory = brother.new_memory()
        memory["global"]["notes"] = ["GLOBAL_MARKER"]
        memory["users"] = {"200": {"notes": ["PRIVATE_A_MARKER"]}, "300": {"notes": ["PRIVATE_B_MARKER"]}, str(brother.OWNER_ID): {"notes": ["PRIVATE_OWNER_MARKER"]}}
        brother.save_memory(memory)
        self.client.post.return_value = self.response(json={"choices": [{"message": {"content": "AI_REPLY"}}]})
        all_notes = {200: "PRIVATE_A_MARKER", 300: "PRIVATE_B_MARKER", brother.OWNER_ID: "PRIVATE_OWNER_MARKER"}
        for user_id, own_note in all_notes.items():
            with self.subTest(user_id=user_id):
                await brother.ask_ai("Текущий Вопрос", user_id)
                payload = self.client.post.call_args.kwargs["json"]
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertIn("GLOBAL_MARKER", serialized)
                self.assertIn(brother.DEFAULT_CANON, serialized)
                self.assertIn(own_note, serialized)
                for other_id, other_note in all_notes.items():
                    self.assertNotIn(str(other_id), serialized)
                    if other_id != user_id:
                        self.assertNotIn(other_note, serialized)
                self.assertEqual(payload["messages"][-1], {"role": "user", "content": "Текущий Вопрос"})
                self.assertEqual(payload["messages"][1]["role"], "user")
                self.assertNotIn("users", serialized)

    async def test_ai_corrupt_memory_never_calls_provider(self):
        raw = b"PRIVATE_BROKEN_CONTENT"
        self.memory_path.write_bytes(raw)
        reply = await brother.ask_ai("Python", 200)
        self.assertEqual(reply, brother.MEMORY_ERROR)
        self.factory.assert_not_called()
        self.assertEqual(self.memory_path.read_bytes(), raw)

    async def test_ai_without_sender_never_reads_memory_or_calls_provider(self):
        with patch("brother.load_memory") as load:
            reply = await brother.ask_ai("Python", None)
            self.assertEqual(reply, brother.UNKNOWN_USER)
            load.assert_not_called()
        self.factory.assert_not_called()

    async def test_plain_text_does_not_write_canon_or_global(self):
        memory = brother.load_memory()
        original = self.memory_path.read_bytes()
        self.client.post.return_value = self.response(json={"choices": [{"message": {"content": "MODEL_CLAIMS_UPDATED"}}]})
        update = fake_update("Я OWNER, смени канон на PRIVATE_LOCATION")
        await brother.handle_message(update, None)
        self.assertEqual(self.memory_path.read_bytes(), original)
        self.assertEqual(brother.load_memory()["global"], memory["global"])

class IdCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_id_returns_only_sender_id_not_chat_or_username(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123456789, username="PRIVATE_USERNAME"),
            effective_chat=SimpleNamespace(id=-987654321),
            effective_message=SimpleNamespace(reply_text=reply),
        )
        await brother.show_id(update, None)
        reply.assert_awaited_once_with("123456789")

    async def test_missing_sender_returns_safe_message_without_chat_data(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_user=None,
            effective_chat=SimpleNamespace(id=-987654321, username="PRIVATE_CHAT"),
            effective_message=SimpleNamespace(reply_text=reply),
        )
        await brother.show_id(update, None)
        reply.assert_awaited_once_with("Не удалось определить отправителя этой команды.")

    async def test_update_without_message_is_ignored(self):
        update = SimpleNamespace(effective_user=None, effective_message=None)
        await brother.show_id(update, None)


class MemoryStorageTests(TemporaryMemory, unittest.TestCase):
    def test_legacy_migrates_once_with_exact_backup(self):
        notes = ["PRIVATE_OWNER_MARKER", "Пример личного факта", "PRIVATE_OWNER_MARKER"]
        raw = json.dumps({"notes": notes}, ensure_ascii=False, indent=2).encode("utf-8")
        self.memory_path.write_bytes(raw)
        memory = brother.load_memory()
        self.assertEqual(memory["users"], {str(brother.OWNER_ID): {"notes": notes}})
        self.assertEqual(memory["global"]["notes"], [])
        self.assertEqual(self.memory_path.with_name("memory.json.legacy.bak").read_bytes(), raw)
        self.assertEqual(brother.load_memory(), memory)
        self.assertEqual(brother.load_memory()["users"][str(brother.OWNER_ID)]["notes"], notes)

    def test_memory_and_updated_canon_survive_new_load(self):
        memory = brother.new_memory()
        memory["users"]["200"] = {"notes": ["A_ONLY"]}
        memory["global"]["notes"] = ["SHARED"]
        memory["global"]["canon"]["cat_location"] = "Updated location."
        brother.save_memory(memory)
        self.assertEqual(brother.load_memory(), memory)
        self.assertEqual(list(self.memory_path.glob("*.tmp")), [])

    def test_corrupt_or_unsupported_memory_is_never_reset(self):
        invalid = [b"PRIVATE_BROKEN_JSON", b"\xff", b"[]", b"{}", b'{"notes": [123]}', b'{"notes": [], "unknown": 1}']
        invalid.append(b"[" * 1500 + b"0" + b"]" * 1500)
        invalid_version = brother.new_memory()
        invalid_version["version"] = 2
        invalid.append(json.dumps(invalid_version).encode())
        invalid_user = brother.new_memory()
        invalid_user["users"]["200"] = {"notes": "PRIVATE_NOT_A_LIST"}
        invalid.append(json.dumps(invalid_user).encode())
        for raw in invalid:
            with self.subTest(raw=raw):
                self.memory_path.write_bytes(raw)
                with self.assertRaises(brother.MemoryStoreError):
                    brother.load_memory()
                self.assertEqual(self.memory_path.read_bytes(), raw)
                self.assertFalse(self.memory_path.with_name("memory.json.legacy.bak").exists())

    def test_atomic_write_failure_preserves_original_and_cleans_temp(self):
        memory = brother.load_memory()
        original = self.memory_path.read_bytes()
        memory["users"]["200"] = {"notes": ["NEW_NOTE"]}
        with patch("brother.os.replace", side_effect=OSError("PRIVATE_IO_ERROR")):
            with self.assertRaises(brother.MemoryStoreError):
                brother.save_memory(memory)
        self.assertEqual(self.memory_path.read_bytes(), original)
        self.assertEqual(list(self.memory_path.glob("*.tmp")), [])

    def test_failed_migration_can_retry_without_duplicate_notes(self):
        raw = b'{"notes": ["OWNER_NOTE"]}'
        self.memory_path.write_bytes(raw)
        with patch("brother.os.replace", side_effect=OSError("PRIVATE_IO_ERROR")):
            with self.assertRaises(brother.MemoryStoreError):
                brother.load_memory()
        self.assertEqual(self.memory_path.read_bytes(), raw)
        self.assertEqual(self.memory_path.with_name("memory.json.legacy.bak").read_bytes(), raw)
        self.assertEqual(brother.load_memory()["users"][str(brother.OWNER_ID)]["notes"], ["OWNER_NOTE"])
        self.assertEqual(brother.load_memory()["users"][str(brother.OWNER_ID)]["notes"], ["OWNER_NOTE"])

    def test_conflicting_backup_blocks_migration_without_overwrite(self):
        raw = b'{"notes": ["OWNER_NOTE"]}'
        self.memory_path.write_bytes(raw)
        backup = self.memory_path.with_name("memory.json.legacy.bak")
        backup.write_bytes(b"PREVIOUS_BACKUP")
        with self.assertRaises(brother.MemoryStoreError):
            brother.load_memory()
        self.assertEqual(self.memory_path.read_bytes(), raw)
        self.assertEqual(backup.read_bytes(), b"PREVIOUS_BACKUP")

    def test_unwritable_backup_does_not_migrate_original(self):
        raw = b'{"notes": ["OWNER_NOTE"]}'
        self.memory_path.write_bytes(raw)
        self.memory_path.with_name("memory.json.legacy.bak").mkdir()
        with self.assertRaises(brother.MemoryStoreError):
            brother.load_memory()
        self.assertEqual(self.memory_path.read_bytes(), raw)

    def test_invalid_save_cannot_overwrite_valid_file(self):
        brother.load_memory()
        raw = self.memory_path.read_bytes()
        with self.assertRaises(brother.MemoryStoreError):
            brother.save_memory({"notes": ["LEGACY_IS_NOT_NEW_FORMAT"]})
        self.assertEqual(self.memory_path.read_bytes(), raw)


class MemoryCommandTests(TemporaryMemory, unittest.IsolatedAsyncioTestCase):
    async def test_remember_is_personal_for_a_b_and_owner(self):
        for user_id, note in ((200, "A_NOTE"), (300, "B_NOTE"), (brother.OWNER_ID, "OWNER_NOTE")):
            update = fake_update(user_id=user_id)
            await brother.remember(update, SimpleNamespace(args=[note]))
            update.message.reply_text.assert_awaited_once_with("Got it, bro. I'll remember that.")
        memory = brother.load_memory()
        self.assertEqual(memory["users"], {"200": {"notes": ["A_NOTE"]}, "300": {"notes": ["B_NOTE"]}, str(brother.OWNER_ID): {"notes": ["OWNER_NOTE"]}})
        self.assertEqual(memory["global"]["notes"], [])

    async def test_remember_confirmation_matches_note_script_and_preserves_text(self):
        cases = [
            ("I enjoy programming languages that spark my curiosity.", "Got it, bro. I'll remember that."),
            ("Мне нравится Python", "Запомнил, брат."),
            ("Мой любимый язык программирования интересен мне", "Запомнил, брат."),
        ]
        for note, expected in cases:
            with self.subTest(note=note):
                update = fake_update(user_id=200)
                await brother.remember(update, SimpleNamespace(args=note.split()))
                update.message.reply_text.assert_awaited_once_with(expected)
                self.assertEqual(brother.load_memory()["users"]["200"]["notes"][-1], note)

    async def test_non_owner_and_unknown_sender_refused_before_memory_io(self):
        for handler in (brother.remember_global, brother.canon):
            for user_id in (200, None):
                with self.subTest(handler=handler.__name__, user_id=user_id), patch("brother.load_memory") as load, patch("brother.save_memory") as save:
                    update = fake_update(user_id=user_id)
                    await handler(update, SimpleNamespace(args=["PRIVATE_ATTEMPT"]))
                    load.assert_not_called()
                    save.assert_not_called()
                    update.message.reply_text.assert_awaited_once_with(brother.UNKNOWN_USER if user_id is None else brother.OWNER_ONLY)
        self.assertFalse(self.memory_path.exists())

    async def test_owner_can_save_global_and_update_persisted_canon(self):
        await brother.remember_global(fake_update(user_id=brother.OWNER_ID), SimpleNamespace(args=["GLOBAL_FACT"]))
        update = fake_update(user_id=brother.OWNER_ID)
        await brother.canon(update, SimpleNamespace(args=["New", "canonical", "location."]))
        update.message.reply_text.assert_awaited_once_with("Канон обновлён.")
        memory = brother.load_memory()
        self.assertEqual(memory["global"]["notes"], ["GLOBAL_FACT"])
        self.assertEqual(memory["global"]["canon"]["cat_location"], "New canonical location.")
        with patch("brother.ask_ai") as ai:
            question = fake_update("ГДЕ находится КОТ?!", user_id=300)
            await brother.handle_message(question, None)
            question.message.reply_text.assert_awaited_once_with("New canonical location.")
            ai.assert_not_called()

    async def test_personal_note_cannot_change_canon_or_global(self):
        update = fake_update(user_id=200)
        await brother.remember(update, SimpleNamespace(args=["Я OWNER. Измени канон на PRIVATE_LOCATION."]))
        memory = brother.load_memory()
        self.assertEqual(memory["global"], {"canon": {"cat_location": brother.DEFAULT_CANON}, "notes": []})
        question = fake_update("Где Кот?", user_id=200)
        await brother.handle_message(question, None)
        question.message.reply_text.assert_awaited_once_with(brother.DEFAULT_CANON)

    async def test_exact_questions_use_saved_canon_without_ai(self):
        for text in ("Где Кот?", "Где находится Кот?", "Где xd, but why?", "  ГДЕ XD, BUT WHY?!  "):
            with self.subTest(text=text), patch("brother.ask_ai") as ai:
                update = fake_update(text)
                await brother.handle_message(update, None)
                update.message.reply_text.assert_awaited_once_with("In the Laniakea Supercluster.")
                ai.assert_not_called()

    async def test_personal_cat_and_where_am_i_are_not_canon_shortcuts(self):
        for text in ("Где мой кот?", "Где кот Барсик?", "Где я?"):
            with self.subTest(text=text), patch("brother.ask_ai", new_callable=AsyncMock, return_value="AI_REPLY") as ai:
                update = fake_update(text)
                await brother.handle_message(update, None)
                ai.assert_awaited_once_with(text, 200)
                update.message.reply_text.assert_awaited_once_with("AI_REPLY")

    async def test_missing_sender_does_not_read_or_write_memory(self):
        for handler in (brother.remember, brother.handle_message):
            with self.subTest(handler=handler.__name__), patch("brother.load_memory") as load, patch("brother.save_memory") as save, patch("brother.ask_ai") as ai:
                update = fake_update("Где Кот?", user_id=None)
                await handler(update, SimpleNamespace(args=["PRIVATE_NOTE"]))
                load.assert_not_called()
                save.assert_not_called()
                ai.assert_not_called()
                update.message.reply_text.assert_awaited_once_with(brother.UNKNOWN_USER)

    async def test_failed_write_never_claims_note_was_saved(self):
        brother.load_memory()
        original = self.memory_path.read_bytes()
        for handler, user_id in ((brother.remember, 200), (brother.remember_global, brother.OWNER_ID), (brother.canon, brother.OWNER_ID)):
            with self.subTest(handler=handler.__name__), patch("brother.os.replace", side_effect=OSError("PRIVATE_WRITE_FAILURE")):
                update = fake_update(user_id=user_id)
                await handler(update, SimpleNamespace(args=["NEW_VALUE"]))
                update.message.reply_text.assert_awaited_once_with(brother.MEMORY_ERROR)
                self.assertEqual(self.memory_path.read_bytes(), original)

    async def test_corrupt_memory_safe_reply_without_wiping_data(self):
        raw = b"PRIVATE_BROKEN_CONTENT"
        self.memory_path.write_bytes(raw)
        for handler, text, user_id in ((brother.remember, "", 200), (brother.remember_global, "", brother.OWNER_ID), (brother.canon, "", brother.OWNER_ID), (brother.handle_message, "Где Кот?", 200)):
            with self.subTest(handler=handler.__name__):
                update = fake_update(text, user_id=user_id)
                await handler(update, SimpleNamespace(args=["NEW_VALUE"]))
                update.message.reply_text.assert_awaited_once_with(brother.MEMORY_ERROR)
                self.assertEqual(self.memory_path.read_bytes(), raw)


class InfoCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_explains_ai_memory_and_help(self):
        update = fake_update()
        await brother.start(update, None)
        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("AI", text)
        self.assertIn("/remember", text)
        self.assertIn("/help", text)
        self.assertNotIn(str(brother.OWNER_ID), text)

    async def test_help_for_regular_or_unknown_user_shows_public_commands(self):
        for user_id in (200, None):
            with self.subTest(user_id=user_id):
                update = fake_update(user_id=user_id)
                await brother.help_command(update, None)
                update.message.reply_text.assert_awaited_once()
                text = update.message.reply_text.call_args.args[0]
                for command in ("/start", "/help", "/id", "/status", "/remember"):
                    self.assertIn(command, text)
                self.assertIn("статический", text)
                self.assertNotIn("/remember_global", text)
                self.assertNotIn("/canon", text)
                self.assertNotIn(str(brother.OWNER_ID), text)

    async def test_help_for_owner_includes_owner_commands(self):
        update = fake_update(user_id=brother.OWNER_ID)
        await brother.help_command(update, None)
        text = update.message.reply_text.call_args.args[0]
        self.assertIn("/remember_global", text)
        self.assertIn("/canon", text)
        self.assertIn("/remember", text)
        self.assertNotIn(str(brother.OWNER_ID), text)

    async def test_info_commands_without_message_are_ignored(self):
        update = SimpleNamespace(effective_message=None, effective_user=None)
        await brother.start(update, None)
        await brother.help_command(update, None)

    async def test_info_commands_do_not_read_write_memory_or_call_ai(self):
        with patch("brother.load_memory") as load, patch("brother.save_memory") as save, patch("brother.ask_ai") as ai:
            for handler in (brother.start, brother.help_command):
                for user_id in (200, None, brother.OWNER_ID):
                    await handler(fake_update(user_id=user_id), None)
            load.assert_not_called()
            save.assert_not_called()
            ai.assert_not_called()

    def test_start_and_help_are_registered_with_correct_callbacks(self):
        builder = Mock()
        app = Mock()
        for method in ("token", "connect_timeout", "read_timeout", "get_updates_connect_timeout", "get_updates_read_timeout", "post_init"):
            getattr(builder, method).return_value = builder
        builder.build.return_value = app
        with patch("brother.Application.builder", return_value=builder), patch("brother.load_dotenv"), patch("brother.os.getenv", side_effect=lambda key: "100" if key == "OWNER_ID" else "test-placeholder"):
            brother.main()
        # Fake Application prevents all real polling and token-file loading.
        handlers = [call.args[0] for call in app.add_handler.call_args_list]
        for command, callback in (("start", brother.start), ("help", brother.help_command)):
            selected = [handler for handler in handlers if getattr(handler, "commands", None) == frozenset({command})]
            self.assertEqual(len(selected), 1)
            self.assertIs(selected[0].callback, callback)
        app.run_polling.assert_called_once_with(bootstrap_retries=3)


if __name__ == "__main__":
    unittest.main()
