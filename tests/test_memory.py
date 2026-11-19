import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from albert import config, memory
from main import app

PHONE = "+41791234567"
BANK_ID = "user-+41791234567"
MASKED_BANK_ID = "user-+4179***67"
RECALLED = {"assistantOverrides": {"model": {"messages": [{"role": "system", "content": "Anna likes tea."}]}}}


class FakeHindsight:
    """Stands in for hindsight_client.Hindsight; behaviour is set via class attributes per test."""

    instances = []
    listing = ()          # bank ids returned by list_banks
    delay = 0             # seconds list_banks hangs
    list_error = None     # exception raised by list_banks
    create_error = None   # exception raised by acreate_bank
    delete_error = None   # exception raised by adelete_bank

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.list_queries = []
        self.created = []
        self.deleted = []
        self.calls = []       # ("delete" | "create", bank id) in order
        self.banks = SimpleNamespace(list_banks=self.list_banks)
        FakeHindsight.instances.append(self)

    async def list_banks(self, q=None):
        self.list_queries.append(q)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.list_error:
            raise self.list_error
        return SimpleNamespace(banks=[SimpleNamespace(bank_id=bank_id) for bank_id in self.listing])

    async def acreate_bank(self, bank_id):
        if self.create_error:
            raise self.create_error
        self.created.append(bank_id)
        self.calls.append(("create", bank_id))

    async def adelete_bank(self, bank_id):
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(bank_id)
        self.calls.append(("delete", bank_id))

    async def aclose(self):
        self.closed = True


class FakeWebhook:
    """Stands in for hindsight_vapi.HindsightVapiWebhook."""

    instances = []
    wrapped = RECALLED     # result of build_assistant_overrides
    response = {"ok": True}
    handle_error = None
    recall_error = None

    def __init__(self, bank_id, client=None, hindsight_api_url=None, api_key=None):
        self.bank_id = bank_id
        self.hindsight_api_url = hindsight_api_url
        self.api_key = api_key
        self._client = client or FakeHindsight(base_url=hindsight_api_url, api_key=api_key)
        self.recall_queries = []
        self.retained = False
        FakeWebhook.instances.append(self)

    async def build_assistant_overrides(self, query):
        self.recall_queries.append(query)
        if self.recall_error:
            raise self.recall_error
        return self.wrapped

    async def handle(self, event):
        if self.handle_error:
            raise self.handle_error
        asyncio.create_task(self._retain())  # like Hindsight: retention runs in the background
        return self.response

    async def _retain(self):
        await asyncio.sleep(0.01)
        self.retained = True


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        for cls, attrs in ((FakeHindsight, ("listing", "delay", "list_error", "create_error", "delete_error")),
                           (FakeWebhook, ("wrapped", "response", "handle_error", "recall_error"))):
            cls.instances = []
            for attr in attrs:  # drop per-test overrides, back to the class defaults
                self.addCleanup(setattr, cls, attr, getattr(cls, attr))
        for target, fake in (("albert.memory.Hindsight", FakeHindsight),
                             ("albert.memory.HindsightVapiWebhook", FakeWebhook)):
            patcher = patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        ctx = app.app_context()
        ctx.push()
        self.addCleanup(ctx.pop)

    @property
    def client(self):
        self.assertEqual(len(FakeHindsight.instances), 1)
        return FakeHindsight.instances[0]


class HelperTests(unittest.TestCase):
    def test_bank_id_for_prefixes_user(self):
        self.assertEqual(memory.bank_id_for(PHONE), BANK_ID)

    def test_bank_id_for_same_bank_for_different_spellings(self):
        for spelling in ("+41 79 123 45 67", "0041791234567", "+41 (0)79 123 45 67", "0041-79-123.45/67",
                         PHONE + "\n"):
            with self.subTest(spelling=spelling):
                self.assertEqual(memory.bank_id_for(spelling), BANK_ID)

    def test_bank_id_for_unnormalizable_raises_without_number_in_message(self):
        for value in ("anonymous", "Restricted", "079 123 45 67", "41791234567", "{{customer.number}}", "", None, 41791234567):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as cm:
                    memory.bank_id_for(value)
                self.assertEqual(str(cm.exception), "Not a normalizable phone number; refusing to build a memory bank id.")
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(cm.exception))

    def test_memory_text_with_messages_returns_first_content(self):
        self.assertEqual(memory.memory_text(RECALLED), "Anna likes tea.")

    def test_memory_text_without_messages_returns_no_memory_text(self):
        for wrapped in ({}, {"assistantOverrides": {}}, {"assistantOverrides": {"model": {}}},
                        {"assistantOverrides": {"model": {"messages": []}}}):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(memory.memory_text(wrapped), config.NO_MEMORY_TEXT)

    def test_memory_text_empty_content_returns_no_memory_text(self):
        wrapped = {"assistantOverrides": {"model": {"messages": [{"content": ""}]}}}
        self.assertEqual(memory.memory_text(wrapped), config.NO_MEMORY_TEXT)

    def test_memory_text_rejects_malformed_response_shapes(self):
        malformed = (
            (None, "Hindsight memory response must be an object."),
            ([], "Hindsight memory response must be an object."),
            ("x", "Hindsight memory response must be an object."),
            ({"assistantOverrides": []},
             "Hindsight memory response field 'assistantOverrides' must be an object."),
            ({"assistantOverrides": {"model": []}},
             "Hindsight memory response field 'assistantOverrides.model' must be an object."),
            ({"assistantOverrides": {"model": {"messages": {}}}},
             "Hindsight memory response field 'assistantOverrides.model.messages' must be an array."),
            ({"assistantOverrides": {"model": {"messages": [None]}}},
             "Hindsight memory response first message must be an object with string 'content'."),
            ({"assistantOverrides": {"model": {"messages": [{"content": None}]}}},
             "Hindsight memory response first message must be an object with string 'content'."),
        )
        for wrapped, expected in malformed:
            with self.subTest(wrapped=wrapped), self.assertRaises(ValueError) as cm:
                memory.memory_text(wrapped)
            self.assertEqual(str(cm.exception), expected)


class ClientTests(MemoryTestCase):
    def test_client_gets_url_key_timeout_and_single_attempt(self):
        memory._client("key-1", 0.7)
        self.assertEqual(self.client.kwargs, {"base_url": config.HINDSIGHT_API_URL, "api_key": "key-1",
                                              "timeout": 0.7, "max_attempts": 1})


class EnsureBankTests(MemoryTestCase):
    def test_ensure_bank_uses_normalized_bank_id(self):
        self.assertIs(memory.ensure_bank("0041 79 123 45 67", "key-1", 0.2), True)
        self.assertEqual((self.client.list_queries, self.client.created), ([BANK_ID], [BANK_ID]))

    def test_ensure_bank_unnormalizable_number_raises_value_error(self):
        with self.assertRaises(ValueError):
            memory.ensure_bank("079 123 45 67", "key-1", 0.2)
        for client in FakeHindsight.instances:
            self.assertEqual((client.list_queries, client.created), ([], []))

    def test_ensure_bank_existing_returns_false_without_create(self):
        FakeHindsight.listing = (BANK_ID,)
        self.assertIs(memory.ensure_bank(PHONE, "key-1", 0.2), False)
        self.assertEqual((self.client.created, self.client.closed), ([], True))
        self.assertEqual(self.client.list_queries, [BANK_ID])

    def test_ensure_bank_missing_creates_and_returns_true(self):
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertIs(memory.ensure_bank(PHONE, "key-1", 0.2), True)
        self.assertEqual((self.client.created, self.client.closed), ([BANK_ID], True))
        self.assertIn(MASKED_BANK_ID, "\n".join(logs.output))
        self.assertNotIn(PHONE, "\n".join(logs.output))

    def test_ensure_bank_only_similar_bank_listed_creates(self):
        FakeHindsight.listing = (BANK_ID + "1", "user-" + PHONE[:-1])
        self.assertIs(memory.ensure_bank(PHONE, "key-1", 0.2), True)
        self.assertEqual(self.client.created, [BANK_ID])

    def test_ensure_bank_client_gets_timeout(self):
        memory.ensure_bank(PHONE, "key-1", 0.2)
        self.assertEqual((self.client.kwargs["timeout"], self.client.kwargs["max_attempts"]), (0.2, 1))

    def test_ensure_bank_hanging_raises_timeout_and_closes(self):
        FakeHindsight.delay = 5
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            memory.ensure_bank(PHONE, "key-1", 0.2)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual((self.client.created, self.client.closed), ([], True))

    def test_ensure_bank_list_error_propagates_and_closes(self):
        FakeHindsight.list_error = RuntimeError("401 unauthorized")
        with self.assertRaisesRegex(RuntimeError, "401"):
            memory.ensure_bank(PHONE, "key-1", 0.2)
        self.assertTrue(self.client.closed)

    def test_ensure_bank_create_error_propagates_and_closes(self):
        FakeHindsight.create_error = RuntimeError("500")
        with self.assertLogs(app.logger, "INFO"), self.assertRaises(RuntimeError):
            memory.ensure_bank(PHONE, "key-1", 0.2)
        self.assertTrue(self.client.closed)


class DeleteBankTests(MemoryTestCase):
    def test_delete_bank_existing_deleted_not_recreated(self):
        FakeHindsight.listing = (BANK_ID,)
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertIsNone(memory.delete_bank("0041 79 123 45 67", "key-1", 0.2))
        self.assertEqual((self.client.list_queries, self.client.calls, self.client.closed),
                         ([BANK_ID], [("delete", BANK_ID)], True))
        self.assertIn(MASKED_BANK_ID, "\n".join(logs.output))
        self.assertNotIn(PHONE, "\n".join(logs.output))

    def test_delete_bank_missing_is_fine(self):
        FakeHindsight.listing = (BANK_ID + "1",)
        memory.delete_bank(PHONE, "key-1", 0.2)
        self.assertEqual((self.client.calls, self.client.closed), ([], True))

    def test_delete_bank_unnormalizable_number_raises_value_error(self):
        with self.assertRaises(ValueError):
            memory.delete_bank("079 123 45 67", "key-1", 0.2)
        self.assertEqual(FakeHindsight.instances, [])

    def test_delete_bank_client_gets_timeout(self):
        memory.delete_bank(PHONE, "key-1", 0.2)
        self.assertEqual((self.client.kwargs["timeout"], self.client.kwargs["max_attempts"]), (0.2, 1))

    def test_delete_bank_hanging_raises_timeout_and_closes(self):
        FakeHindsight.listing, FakeHindsight.delay = (BANK_ID,), 5
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            memory.delete_bank(PHONE, "key-1", 0.2)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual((self.client.calls, self.client.closed), ([], True))

    def test_delete_bank_error_propagates_and_closes(self):
        FakeHindsight.listing, FakeHindsight.delete_error = (BANK_ID,), RuntimeError("500")
        with self.assertRaisesRegex(RuntimeError, "500"):
            memory.delete_bank(PHONE, "key-1", 0.2)
        self.assertTrue(self.client.closed)


class EraseMemoryTests(MemoryTestCase):
    def test_erase_memory_deletes_then_creates_empty_bank(self):
        FakeHindsight.listing = (BANK_ID,)
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertIsNone(memory.erase_memory("0041 79 123 45 67", "key-1", 0.2))
        self.assertEqual((self.client.calls, self.client.closed), ([("delete", BANK_ID), ("create", BANK_ID)], True))
        self.assertNotIn(PHONE, "\n".join(logs.output))

    def test_erase_memory_missing_bank_just_created(self):
        with self.assertLogs(app.logger, "INFO"):
            memory.erase_memory(PHONE, "key-1", 0.2)
        self.assertEqual(self.client.calls, [("create", BANK_ID)])

    def test_erase_memory_unnormalizable_number_raises_value_error(self):
        with self.assertRaises(ValueError):
            memory.erase_memory("anonymous", "key-1", 0.2)
        self.assertEqual(FakeHindsight.instances, [])

    def test_erase_memory_delete_error_propagates_without_create(self):
        FakeHindsight.listing, FakeHindsight.delete_error = (BANK_ID,), RuntimeError("500")
        with self.assertRaises(RuntimeError):
            memory.erase_memory(PHONE, "key-1", 0.2)
        self.assertEqual((self.client.created, self.client.closed), ([], True))

    def test_erase_memory_create_error_propagates_and_closes(self):
        FakeHindsight.listing, FakeHindsight.create_error = (BANK_ID,), RuntimeError("500")
        with self.assertLogs(app.logger, "INFO"), self.assertRaises(RuntimeError):
            memory.erase_memory(PHONE, "key-1", 0.2)
        self.assertEqual((self.client.deleted, self.client.closed), ([BANK_ID], True))

    def test_erase_memory_hanging_raises_timeout(self):
        FakeHindsight.delay = 5
        with self.assertRaises(TimeoutError):
            memory.erase_memory(PHONE, "key-1", 0.2)
        self.assertEqual((self.client.calls, self.client.closed), ([], True))


class RecallForPhoneTests(MemoryTestCase):
    def test_recall_existing_bank_returns_memory_not_created(self):
        FakeHindsight.listing = (BANK_ID,)
        self.assertEqual(memory.recall_for_phone(PHONE, "key-1", 0.2), ("Anna likes tea.", False))
        self.assertEqual((self.client.created, self.client.closed), ([], True))
        self.assertEqual(len(FakeWebhook.instances[0].recall_queries), 1)

    def test_recall_webhook_uses_bank_id_and_same_client(self):
        FakeHindsight.listing = (BANK_ID,)
        memory.recall_for_phone(PHONE, "key-1", 0.2)
        webhook = FakeWebhook.instances[0]
        self.assertEqual((webhook.bank_id, webhook._client), (BANK_ID, self.client))
        self.assertEqual((self.client.kwargs["timeout"], self.client.kwargs["max_attempts"]), (0.2, 1))

    def test_recall_existing_bank_without_memories_returns_no_memory_text(self):
        FakeHindsight.listing = (BANK_ID,)
        FakeWebhook.wrapped = {}
        self.assertEqual(memory.recall_for_phone(PHONE, "key-1", 0.2), (config.NO_MEMORY_TEXT, False))

    def test_recall_missing_bank_creates_and_returns_no_memory_text(self):
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertEqual(memory.recall_for_phone(PHONE, "key-1", 0.2), (config.NO_MEMORY_TEXT, True))
        self.assertEqual((self.client.created, self.client.closed), ([BANK_ID], True))
        self.assertEqual(FakeWebhook.instances[0].recall_queries, [])
        self.assertIn(MASKED_BANK_ID, "\n".join(logs.output))
        self.assertNotIn(PHONE, "\n".join(logs.output))

    def test_recall_only_similar_bank_listed_creates(self):
        FakeHindsight.listing = (BANK_ID + "1",)
        with self.assertLogs(app.logger, "INFO"):
            self.assertEqual(memory.recall_for_phone(PHONE, "key-1", 0.2), (config.NO_MEMORY_TEXT, True))
        self.assertEqual(self.client.created, [BANK_ID])

    def test_recall_hanging_raises_timeout_within_deadline_and_closes(self):
        FakeHindsight.delay = 5
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            memory.recall_for_phone(PHONE, "key-1", 0.2)
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(self.client.closed)

    def test_recall_errors_propagate_and_close(self):
        for attr, cls in (("list_error", FakeHindsight), ("recall_error", FakeWebhook)):
            with self.subTest(attr):
                FakeHindsight.instances = []
                FakeHindsight.listing = (BANK_ID,)
                setattr(cls, attr, RuntimeError("boom"))
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    memory.recall_for_phone(PHONE, "key-1", 0.2)
                self.assertTrue(self.client.closed)
                setattr(cls, attr, None)

    def test_recall_create_error_propagates_and_closes(self):
        FakeHindsight.create_error = RuntimeError("boom")
        with self.assertLogs(app.logger, "INFO"), self.assertRaisesRegex(RuntimeError, "boom"):
            memory.recall_for_phone(PHONE, "key-1", 0.2)
        self.assertTrue(self.client.closed)


class ProcessEventTests(MemoryTestCase):
    def test_process_event_end_of_call_report_waits_for_retention(self):
        event = {"message": {"type": "end-of-call-report"}}
        self.assertEqual(memory.process_event(event, BANK_ID, "key-1"), {"ok": True})
        webhook = FakeWebhook.instances[0]
        self.assertTrue(webhook.retained)
        self.assertTrue(webhook._client.closed)

    def test_process_event_builds_webhook_for_bank(self):
        memory.process_event({"message": {"type": "status-update"}}, BANK_ID, "key-1")
        webhook = FakeWebhook.instances[0]
        self.assertEqual((webhook.bank_id, webhook.hindsight_api_url, webhook.api_key),
                         (BANK_ID, config.HINDSIGHT_API_URL, "key-1"))

    def test_process_event_rejects_malformed_webhook_response_and_closes(self):
        for response in ([], "ok", 1):
            with self.subTest(response=response):
                FakeWebhook.instances = []
                FakeWebhook.response = response
                with self.assertRaises(ValueError) as cm:
                    memory.process_event({"message": {"type": "status-update"}}, BANK_ID, "key-1")
                self.assertEqual(str(cm.exception), "Hindsight webhook response must be an object or null.")
                self.assertTrue(FakeWebhook.instances[0]._client.closed)

    def test_process_event_other_events_do_not_wait_but_close(self):
        for event in ({"message": {"type": "status-update"}}, {"message": {}}, {}):
            with self.subTest(event=event):
                FakeWebhook.instances = []
                memory.process_event(event, BANK_ID, "key-1")
                webhook = FakeWebhook.instances[0]
                self.assertFalse(webhook.retained)
                self.assertTrue(webhook._client.closed)

    def test_process_event_empty_response_returns_empty_dict(self):
        FakeWebhook.response = None
        self.assertEqual(memory.process_event({"message": {"type": "end-of-call-report"}}, BANK_ID, "key-1"), {})

    def test_process_event_handle_error_propagates_and_closes(self):
        FakeWebhook.handle_error = RuntimeError("boom")
        with self.assertRaisesRegex(RuntimeError, "boom"):
            memory.process_event({"message": {"type": "end-of-call-report"}}, BANK_ID, "key-1")
        self.assertTrue(FakeWebhook.instances[0]._client.closed)


if __name__ == "__main__":
    unittest.main()
