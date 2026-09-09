import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Obj
import unittest

import bridge


class BridgeTest(unittest.TestCase):
    def test_queue_routes_and_loop_guards(self):
        with TemporaryDirectory() as directory:
            route = {'discord_channel_id': '123', 'kaede_channel_ref': '456@chat.example'}
            store = bridge.Store(Path(directory) / 'bridge.sqlite3')
            store.add_pair(route['kaede_channel_ref'], '10@chat.example', route['discord_channel_id'])
            handlers = {}
            bot = Obj(listen=lambda name: lambda handler: handlers.update({name: handler}))
            client = bridge.Bridge(store, bot)
            message = Obj(guild=True, author=Obj(bot=False, display_name='Alice'),
                          webhook_id=None, channel=Obj(id=123), id=789,
                          clean_content='hello', attachments=[])
            asyncio.run(client.on_message(message))
            asyncio.run(client.on_message(message))
            self.assertEqual(len(store.pending()), 1)
            message.author.bot = True
            message.id = 790
            asyncio.run(client.on_message(message))
            self.assertEqual(len(store.pending()), 1)
            incoming = Obj(author=Obj(bot=False, handle='bob@chat.example'),
                           bot_installation_id=None, application_ref=None, webhook_ref=None,
                           e2ee=None, content_unavailable=False, channel_ref='456@chat.example',
                           ref='999@chat.example', content='reply', attachments=[])
            asyncio.run(handlers['on_message'](incoming))
            self.assertEqual(len(store.pending()), 2)
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
                             bridge.chunks('Discord · Alice', 'hello'))
            self.assertEqual(len(reopened.pending()), 1)
            reopened.engine.dispose()
            parts = bridge.chunks('😀' * 150, '😀' * 4000)
            self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 2000 for p in parts))
            self.assertEqual(''.join(p.split('] ', 1)[1] for p in parts), '😀' * 4000)

    def test_delivery_uses_sdk_and_disables_mentions(self):
        from unittest.mock import AsyncMock, patch

        async def check():
            with TemporaryDirectory() as directory:
                store = bridge.Store(Path(directory) / 'bridge.sqlite3')
                bot = Obj(listen=lambda name: lambda handler: None, send_message=AsyncMock())
                client = bridge.Bridge(store, bot)
                channel = Obj(send=AsyncMock())
                store.add_pair('456@chat.example', '10@chat.example', '123')
                store.enqueue('discord:1', 'kaede', '456@chat.example', ['hello'])
                store.enqueue('kaede:2@chat.example', 'discord', '123', ['@everyone'])
                with patch.object(client, 'wait_until_ready', AsyncMock()), \
                     patch.object(client, 'get_channel', return_value=channel), \
                     patch('bridge.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
                    with self.assertRaises(asyncio.CancelledError):
                        await client.deliver()
                self.assertEqual(store.pending(), [])
                self.assertEqual(str(bot.send_message.call_args.args[0]), '456@chat.example')
                self.assertEqual(bot.send_message.call_args.kwargs['allowed_mentions']['parse'], [])
                self.assertEqual(len(bot.send_message.call_args.kwargs['client_nonce']), 64)
                self.assertFalse(channel.send.call_args.kwargs['allowed_mentions'].everyone)
                await client.close()
                store.engine.dispose()
        asyncio.run(check())
