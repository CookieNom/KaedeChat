import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Obj
import unittest

import bridge


class BridgeTest(unittest.TestCase):
    def test_existing_queue_migration_preserves_messages(self):
        import sqlite3
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'bridge.sqlite3'
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE deliveries (sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, platform TEXT NOT NULL, destination TEXT NOT NULL, content TEXT NOT NULL, done INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, retry_at INTEGER DEFAULT 0)")
                conn.execute("INSERT INTO deliveries (id, platform, destination, content) VALUES ('old', 'discord', '123', 'queued')")
            for _ in range(2):
                store = bridge.Store(path)
                self.assertEqual(store.pending()[0]['content'], 'queued')
                self.assertIsNone(store.pending()[0]['author'])
                store.engine.dispose()

    def test_gateway_readiness_and_failures_are_logged(self):
        from unittest.mock import Mock

        async def check():
            handlers = {}
            bot = Obj(listen=lambda name: lambda handler: handlers.update({name: handler}))
            client = bridge.Bridge(Mock(), bot)
            try:
                with self.assertLogs("bridge", level="INFO") as logs:
                    await handlers["on_ready"](Obj(target="https://guild.example", installations=[{}]))
                    for name, kind in (("on_gateway_error", "GATEWAY_ERROR"),
                                       ("on_target_discovery_error", "TARGET_DISCOVERY_ERROR")):
                        await handlers[name](Obj(type=kind, target="https://guild.example",
                                                 data={"error": "received 4401: authentication failed"}))
                self.assertIn("gateway ready on https://guild.example (1 guild installations)", logs.output[0])
                self.assertIn("GATEWAY_ERROR", logs.output[1])
                self.assertIn("authentication failed", logs.output[1])
                self.assertIn("TARGET_DISCOVERY_ERROR", logs.output[2])
            finally:
                await client.close()

        asyncio.run(check())

    def test_enrollment_matches_server_contract(self):
        import ast
        import runpy

        root = Path(__file__).resolve().parents[3]
        contract = root / 'backend/app/bots/application_contract.py'
        if not contract.exists():
            self.skipTest('Server contract check requires the repository checkout')
        tree = ast.parse(contract.read_text())
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'SUPPORTED_APPLICATION_SCOPES'
                                  for target in node.targets))
        scopes = ast.literal_eval(assignment.value.args[0])
        intents = runpy.run_path(str(root / 'backend/app/core/bot_intents.py'))['SUPPORTED_BOT_INTENTS']
        self.assertLessEqual(set(bridge.SCOPES), scopes)
        self.assertLessEqual(set(bridge.INTENTS.names()), intents)
        self.assertIn('messages.metadata', bridge.SCOPES)


    def test_queue_routes_and_loop_guards(self):
        with TemporaryDirectory() as directory:
            route = {'discord_channel_id': '123', 'kaede_channel_ref': '456@chat.example'}
            store = bridge.Store(Path(directory) / 'bridge.sqlite3')
            store.add_pair(route['kaede_channel_ref'], '10@chat.example', route['discord_channel_id'])
            handlers = {}
            bot = Obj(listen=lambda name: lambda handler: handlers.update({name: handler}))
            client = bridge.Bridge(store, bot)
            message = Obj(guild=True, author=Obj(bot=False, display_name='Alice', display_avatar=Obj(url='https://cdn.discordapp.com/avatar.png')),
                          webhook_id=None, channel=Obj(id=123), id=789,
                          clean_content='hello', attachments=[])
            asyncio.run(client.on_message(message))
            asyncio.run(client.on_message(message))
            self.assertEqual(len(store.pending()), 1)
            self.assertEqual(store.pending()[0]['content'], 'hello')
            message.author.bot = True
            message.id = 790
            asyncio.run(client.on_message(message))
            self.assertEqual(len(store.pending()), 1)
            incoming = Obj(author=Obj(bot=False, handle='bob@chat.example', avatar_hash='a' * 64, ref=Obj(domain='chat.example')),
                           bot_installation_id=None, application_ref=None, webhook_ref=None,
                           e2ee=None, content_unavailable=False, channel_ref='456@chat.example',
                           ref='999@chat.example', content='reply', attachments=[])
            asyncio.run(handlers['on_message'](incoming))
            self.assertEqual(len(store.pending()), 2)
            self.assertEqual(store.pending()[0]['avatar'], 'https://cdn.discordapp.com/avatar.png')
            self.assertEqual(store.pending()[1]['avatar'], f'https://chat.example/media/assets/{"a" * 64}/thumbnail_128')
            self.assertEqual(store.pending()[1]['content'], 'reply')
            incoming.e2ee = {'ciphertext': 'secret'}
            incoming.ref = '1000@chat.example'
            asyncio.run(handlers['on_message'](incoming))
            self.assertEqual(len(store.pending()), 2)
            row = store.pending()[0]
            store.finish(row, False)
            self.assertEqual(len(store.pending()), 1)
            store.finish(row, True)
            store.engine.dispose()
            reopened = bridge.Store(Path(directory) / 'bridge.sqlite3')
            reopened.enqueue('discord:789', 'kaede', route['kaede_channel_ref'],
                             bridge.chunks('hello'))
            self.assertEqual(len(reopened.pending()), 1)
            reopened.engine.dispose()
            parts = bridge.chunks('😀' * 4000)
            self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 2000 for p in parts))
            self.assertEqual(''.join(parts), '😀' * 4000)

    def test_delivery_uses_sdk_and_disables_mentions(self):
        from unittest.mock import AsyncMock, patch

        async def check():
            with TemporaryDirectory() as directory:
                store = bridge.Store(Path(directory) / 'bridge.sqlite3')
                bot = Obj(listen=lambda name: lambda handler: None, send_message=AsyncMock())
                client = bridge.Bridge(store, bot)
                channel = Obj(send=AsyncMock())
                store.add_pair('456@chat.example', '10@chat.example', '123')
                store.enqueue('discord:1', 'kaede', '456@chat.example', ['hello'], author='Discord · Alice', avatar='https://cdn.discordapp.com/avatar.png')
                store.enqueue('kaede:2@chat.example', 'discord', '123', ['@everyone'], author='Kaede · Bob', avatar='https://chat.example/media/assets/hash/thumbnail_128')
                with patch.object(client, 'wait_until_ready', AsyncMock()), \
                     patch.object(client, 'get_channel', return_value=channel), \
                     patch('bridge.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await client.deliver()
                self.assertEqual(store.pending(), [])
                self.assertEqual(bot.send_message.call_args.kwargs['embeds'][0].author.name, 'Discord · Alice')
                self.assertEqual(channel.send.call_args.kwargs['embed'].author.icon_url, 'https://chat.example/media/assets/hash/thumbnail_128')
                self.assertEqual(str(bot.send_message.call_args.args[0]), '456@chat.example')
                self.assertEqual(bot.send_message.call_args.kwargs['allowed_mentions']['parse'], [])
                self.assertEqual(len(bot.send_message.call_args.kwargs['client_nonce']), 64)
                self.assertFalse(channel.send.call_args.kwargs['allowed_mentions'].everyone)
                await client.close()
                store.engine.dispose()
        asyncio.run(check())


    def test_handler_failures_are_logged_without_secret_exception_text(self):
        from kaede_bot.errors import ApiError
        with TemporaryDirectory() as directory:
            store = bridge.Store(Path(directory) / 'bridge.sqlite3')
            handlers = {}
            bot = Obj(listen=lambda name: lambda handler: handlers.update({name: handler}))
            client = bridge.Bridge(store, bot)
            error = ApiError(403, 'BOT_SCOPE_REQUIRED', 'private diagnostic content')
            with self.assertLogs('bridge', level='ERROR') as logs:
                asyncio.run(handlers['on_error'](Obj(target='https://chat.example', data={
                    'event_type': 'INTERACTION_CREATE', 'error': error,
                })))
            self.assertIn('BOT_SCOPE_REQUIRED', logs.output[0])
            self.assertNotIn('private diagnostic content', logs.output[0])
            asyncio.run(client.close())
            store.engine.dispose()
