import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as Obj
import unittest
from unittest.mock import AsyncMock

import kaede_bot as k
from sqlalchemy.exc import IntegrityError
from bridge import Store
from pairing import Pairing


def event(user='1@chat.example', guild='10@chat.example', permissions=32, values=()):
    return Obj(user=Obj(ref=k.EntityRef.parse(user)), guild_ref=k.EntityRef.parse(guild),
               member=Obj(permissions=permissions), context='guild', integration_type='guild_install',
               values=values, respond=AsyncMock(), defer=AsyncMock(), edit_original_response=AsyncMock())


class PairingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / 'bridge.sqlite3')
        self.commands = {}
        def command(**definition):
            return lambda handler: self.commands.update({definition['name']: (definition, handler)})
        self.kaede = Obj(command=command, fetch_channels=AsyncMock(), fetch_channel=AsyncMock())
        self.bridge = Obj(kaede=self.kaede, store=self.store, route_lock=asyncio.Lock(), is_ready=lambda: True)
        self.pairing = Pairing(self.bridge, {'1@chat.example'})

    async def asyncTearDown(self):
        self.store.engine.dispose()
        self.directory.cleanup()

    async def test_authorization_pagination_and_owner_binding(self):
        self.assertEqual(set(self.commands), {'bridge-pair', 'bridge-list', 'bridge-unpair'})
        for definition, _ in self.commands.values():
            self.assertEqual(definition['contexts'], ['guild'])
        for denied in (event(user='2@chat.example'), event(permissions=0)):
            await self.commands['bridge-pair'][1](denied)
            denied.respond.assert_awaited_once()
            self.kaede.fetch_channels.assert_not_awaited()
        first = event()
        selected = AsyncMock()
        await self.pairing.picker(first, 'Pick', [(f'channel {i}', i) for i in range(30)], selected)
        view = first.edit_original_response.call_args.kwargs['view']
        self.assertEqual(len(view.rows[0].components[0].options), 25)
        next_id = view.rows[1].components[0].custom_id
        await view.callbacks[next_id](event(user='2@chat.example'))
        self.assertFalse(view.is_finished())
        next_event = event()
        await view.callbacks[next_id](next_event)
        next_view = next_event.edit_original_response.call_args.kwargs['view']
        self.assertEqual(len(next_view.rows[0].components[0].options), 5)
        await next_view.callbacks[next_view.rows[0].components[0].custom_id](event(values=('0',)))
        self.assertEqual(selected.call_args.args[1], 25)
        await view.callbacks[next_id](event())
        self.assertEqual(selected.await_count, 1)

    async def test_full_pairing_and_persistence_and_unpair(self):
        ref, guild_ref = k.EntityRef.parse('456@chat.example'), k.EntityRef.parse('10@chat.example')
        channel = Obj(ref=ref, guild_ref=guild_ref, type=0, e2ee_required=False, permissions=3072, name='general')
        remote = Obj(id=123, name='general', category=None, permissions_for=lambda me: Obj(view_channel=True, send_messages=True))
        guild = Obj(id=20, name='Discord server', me=object(), text_channels=[remote])
        remote.guild = guild
        self.bridge.guilds = [guild]
        self.bridge.get_guild = lambda identifier: guild if identifier == 20 else None
        self.bridge.get_channel = lambda identifier: remote if identifier == 123 else None
        self.kaede.fetch_channels.return_value = [channel]
        self.kaede.fetch_channel.return_value = channel
        step = event()
        await self.commands['bridge-pair'][1](step)
        for _ in range(4):
            view = step.edit_original_response.call_args.kwargs['view']
            step = event(values=('0',))
            await view.callbacks[view.rows[0].components[0].custom_id](step)
        self.assertEqual(self.store.routes()[0]['discord_channel_id'], '123')
        with self.assertRaises(IntegrityError):
            self.store.add_pair('789@chat.example', str(guild_ref), '123')
        self.assertEqual(self.store.routes('10@other.example'), [])
        reopened = Store(Path(self.directory.name) / 'bridge.sqlite3')
        self.assertEqual(len(reopened.routes()), 1)
        reopened.engine.dispose()
        self.store.enqueue('message', 'discord', '123', ['queued text'])
        step = event()
        await self.commands['bridge-unpair'][1](step)
        view = step.edit_original_response.call_args.kwargs['view']
        await view.callbacks[view.rows[0].components[0].custom_id](event(values=('0',)))
        self.assertEqual(self.store.routes(), [])
        self.assertEqual(self.store.pending(), [])
        channel.e2ee_required = True
        self.assertFalse(self.pairing.kaede_channel(channel, guild_ref))
        channel.e2ee_required = False
        self.assertFalse(self.pairing.kaede_channel(channel, k.EntityRef.parse('11@chat.example')))
