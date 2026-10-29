# -*- coding: utf-8 -*-
"""Exercise retry ownership across cloud, gateway and LAN failures."""
import asyncio
from functools import partial
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock

import pytest

from test_property_routing import SOURCE_PATH, load_client

# These methods own the retry state machine, with no real device transports.
# pylint: disable=protected-access


@pytest.mark.github
class PropertyRefreshTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_class = load_client(SOURCE_PATH.read_text(encoding='utf-8'))

    def client(self, route='gw', properties=None):
        if properties is None:
            properties = [('first', 2, 1), ('second', 2, 1)]
        client = self.client_class()
        devices = {did for did, _, _ in properties}
        client._refresh_props_list = {
            f'{did}|{siid}|{piid}': {'did': did, 'siid': siid, 'piid': piid}
            for did, siid, piid in properties}
        client._device_list_cache = {
            did: {'model': 'test.device'} for did in devices}
        client._device_list_gateway = {
            did: {'group_id': 'hub'} for did in devices}
        client._device_list_lan = {did: {} for did in devices}
        reader = AsyncMock(return_value=None)
        client._mips_local = {
            'hub': types.SimpleNamespace(get_prop_async=reader)
        } if route == 'gw' else {}
        client._miot_lan = types.SimpleNamespace(
            init_done=True, get_prop_async=reader)
        client._network = types.SimpleNamespace(network_status=True)
        client._http = types.SimpleNamespace(get_props_async=AsyncMock())
        client._MIoTClient__on_prop_msg = Mock()
        client._refresh_props_retry_count = 0
        client._refresh_props_timer = None
        client._main_loop = types.SimpleNamespace(
            call_later=Mock(return_value=Mock()))
        return client, reader

    async def local(self, client, route):
        return await getattr(
            client, f'_MIoTClient__refresh_props_from_{route}')()

    def assert_wire_queue(self, client, expected_keys):
        pending = client._refresh_props_list
        self.assertEqual(set(expected_keys), set(pending))
        for key, params in pending.items():
            self.assertEqual({'did', 'siid', 'piid'}, set(params))
            did, siid, piid = (params[name] for name in ('did', 'siid', 'piid'))
            self.assertEqual(f'{did}|{siid}|{piid}', key)
        json.dumps(list(pending.values()))

    @staticmethod
    async def cloud_success(params):
        """Use real JSON encoding at the cloud transport boundary."""
        json.dumps({'datasource': 1, 'params': params})
        return [{**item, 'value': 0, 'code': 0} for item in params]

    async def test_local_failure_can_retry_through_json_cloud_transport(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route)
                self.assertFalse(await self.local(client, route))
                self.assertEqual(2, reader.await_count)
                self.assert_wire_queue(client, ['first|2|1', 'second|2|1'])
                client._http.get_props_async.side_effect = self.cloud_success
                self.assertTrue(
                    await client._MIoTClient__refresh_props_from_cloud())
                self.assert_wire_queue(client, [])
                self.assertEqual(2, client._MIoTClient__on_prop_msg.call_count)

    async def test_partial_success_preserves_failed_peer_and_zero_values(self):
        for route in ('gw', 'lan'):
            for value in (0, False):
                with self.subTest(route=route, value=value):
                    client, reader = self.client(route)
                    reader.side_effect = [value, None]
                    self.assertTrue(await self.local(client, route))
                    self.assert_wire_queue(client, ['second|2|1'])
                    actual = client._MIoTClient__on_prop_msg.call_args.kwargs
                    self.assertIs(value, actual['params']['value'])
                    client._http.get_props_async.side_effect = (
                        self.cloud_success)
                    self.assertTrue(
                        await client._MIoTClient__refresh_props_from_cloud())
                    self.assertEqual(
                        2, client._MIoTClient__on_prop_msg.call_count)

    async def test_one_transport_exception_does_not_discard_peer_results(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route)
                reader.side_effect = [TimeoutError('test timeout'), 17]
                self.assertTrue(await self.local(client, route))
                self.assert_wire_queue(client, ['first|2|1'])
                client._MIoTClient__on_prop_msg.assert_called_once_with(
                    params={'did': 'second', 'siid': 2, 'piid': 1, 'value': 17},
                    ctx=None)

    async def test_unavailable_route_does_not_consume_pending_properties(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route)
                if route == 'gw':
                    client._device_list_gateway.pop('first')
                    client._device_list_gateway['second']['group_id'] = 'gone'
                else:
                    client._device_list_lan.clear()
                self.assertFalse(await self.local(client, route))
                reader.assert_not_awaited()
                self.assert_wire_queue(client, ['first|2|1', 'second|2|1'])
                client._http.get_props_async.side_effect = self.cloud_success
                self.assertTrue(
                    await client._MIoTClient__refresh_props_from_cloud())

    async def test_failed_property_rotates_without_bursting_device_reads(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(
                    route, [('first', 2, 1), ('first', 2, 2)])
                self.assertFalse(await self.local(client, route))
                self.assertEqual(1, reader.await_count)
                self.assertEqual(
                    ['first|2|2', 'first|2|1'],
                    list(client._refresh_props_list))
                self.assertFalse(await self.local(client, route))
                self.assertEqual(2, reader.await_count)
                self.assertEqual(
                    [1, 2], [call.kwargs['piid']
                             for call in reader.await_args_list])
                self.assert_wire_queue(client, ['first|2|1', 'first|2|2'])

    async def test_cancellation_restores_requests_and_cancels_reads(self):
        for route in ('cloud', 'gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route)
                started = asyncio.Event()
                stopped = []

                async def wait_for_cancel(ready, finished, **_kwargs):
                    ready.set()
                    try:
                        await asyncio.Future()
                    finally:
                        finished.append(True)

                waiting_read = partial(wait_for_cancel, started, stopped)
                if route == 'cloud':
                    client._http.get_props_async.side_effect = waiting_read
                    coroutine = client._MIoTClient__refresh_props_from_cloud()
                else:
                    reader.side_effect = waiting_read
                    coroutine = self.local(client, route)
                task = asyncio.create_task(coroutine)
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assert_wire_queue(client, ['first|2|1', 'second|2|1'])
                self.assertEqual(1 if route == 'cloud' else 2, len(stopped))

    async def test_inflight_new_request_is_not_overwritten_or_dropped(self):
        for route in ('cloud', 'gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route, [('first', 2, 1)])
                fresh = {'did': 'first', 'siid': 2, 'piid': 1}

                async def queue_fresh_request(owner, request, **_kwargs):
                    owner._refresh_props_list['first|2|1'] = request
                    return None

                requeue = partial(queue_fresh_request, client, fresh)
                if route == 'cloud':
                    client._http.get_props_async.side_effect = requeue
                    await client._MIoTClient__refresh_props_from_cloud()
                else:
                    reader.side_effect = requeue
                    await self.local(client, route)
                self.assertIs(fresh, client._refresh_props_list['first|2|1'])
                self.assert_wire_queue(client, ['first|2|1'])

    async def test_cloud_accepts_only_valid_matching_results(self):
        client, _ = self.client()
        client._http.get_props_async.return_value = [
            None,
            {'did': 'other', 'siid': 2, 'piid': 1, 'value': 7},
            {'did': 'first', 'siid': 2, 'piid': 1, 'value': 4, 'code': -1},
            {'did': 'first', 'siid': 2, 'piid': 1, 'value': None},
            {'did': 'second', 'siid': 2, 'piid': 1, 'value': False, 'code': 0},
            {'did': 'second', 'siid': 2, 'piid': 1, 'value': 9, 'code': 0},
        ]
        self.assertFalse(await client._MIoTClient__refresh_props_from_cloud())
        self.assert_wire_queue(client, ['first|2|1'])
        client._MIoTClient__on_prop_msg.assert_called_once_with(
            params={'did': 'second', 'siid': 2, 'piid': 1,
                    'value': False, 'code': 0}, ctx=None)

    async def test_cloud_batch_limit_keeps_other_pending_work(self):
        client, _ = self.client()
        client._http.get_props_async.side_effect = self.cloud_success
        self.assertTrue(
            await client._MIoTClient__refresh_props_from_cloud(patch_len=1))
        self.assertEqual(1, len(client._refresh_props_list))
        self.assertTrue(await client._MIoTClient__refresh_props_from_cloud())
        self.assert_wire_queue(client, [])
        self.assertEqual(2, client._MIoTClient__on_prop_msg.call_count)

    async def test_transport_failures_exhaust_existing_retry_budget(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route)
                reader.side_effect = TimeoutError('test timeout')

                async def cloud_failure(params):
                    json.dumps({'params': params})
                    return [{**item, 'code': -1} for item in params]

                client._http.get_props_async.side_effect = cloud_failure
                for attempt in range(4):
                    await client._MIoTClient__refresh_props_handler()
                    keys = ['first|2|1', 'second|2|1'] if attempt < 3 else []
                    self.assert_wire_queue(client, keys)
                self.assertEqual(4, client._http.get_props_async.await_count)
                self.assertEqual(8, reader.await_count)
                self.assertEqual(0, client._refresh_props_retry_count)
                self.assertIsNone(client._refresh_props_timer)
                self.assertEqual(3, client._main_loop.call_later.call_count)

    async def test_partial_fallback_then_cloud_recovery_stops_retry_timer(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client, reader = self.client(route)
                reader.side_effect = [None, False]
                client._http.get_props_async.return_value = [
                    {'did': 'first', 'siid': 2, 'piid': 1, 'code': -1},
                    {'did': 'second', 'siid': 2, 'piid': 1, 'code': -1},
                ]
                await client._MIoTClient__refresh_props_handler()
                self.assert_wire_queue(client, ['first|2|1'])
                client._http.get_props_async.side_effect = self.cloud_success
                await client._MIoTClient__refresh_props_handler()
                self.assert_wire_queue(client, [])
                self.assertIsNone(client._refresh_props_timer)
                self.assertEqual(2, reader.await_count)
                self.assertEqual(2, client._MIoTClient__on_prop_msg.call_count)
