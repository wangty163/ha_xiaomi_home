# -*- coding: utf-8 -*-
"""Replay production client methods using isolated in-memory transports."""
import ast
import asyncio
import copy
import pathlib
import traceback
import types
import unittest
from unittest.mock import AsyncMock, Mock

import pytest

# Private methods are the transport state machine exercised by these tests.
# pylint: disable=protected-access


SOURCE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / 'custom_components/xiaomi_home/miot/miot_client.py')


class TopicMatcher(dict):
    """The exact and MQTT wildcard matches used by this client."""

    def get(self, topic, default=None):
        return super().get(topic, default)

    def iter_match(self, topic):
        for subscribed, callback in self.items():
            if subscribed == topic or (
                    subscribed.endswith('/#')
                    and topic.startswith(subscribed[:-1])):
                yield callback


class Transport:
    """In-memory MQTT endpoint with disconnected-loop protection."""

    def __init__(self):
        self.mips_state = True
        self.properties = {}
        self.events = {}
        self.states = {}

    def sub_prop(self, did, handler, siid=None, piid=None, handler_ctx=None):
        if not self.mips_state:
            raise RuntimeError('offline internal loop is closed')
        self.properties[(did, siid, piid)] = (handler, handler_ctx)
        return True

    def unsub_prop(self, did, siid=None, piid=None):
        if not self.mips_state:
            raise RuntimeError('offline internal loop is closed')
        self.properties.pop((did, siid, piid), None)
        return True

    def sub_event(self, did, handler, siid=None, eiid=None, handler_ctx=None):
        self.events[(did, siid, eiid)] = (handler, handler_ctx)

    def unsub_event(self, did, siid=None, eiid=None):
        self.events.pop((did, siid, eiid), None)

    def sub_device_state(self, did, handler):
        self.states[did] = handler

    def property(self, did, siid, piid, value):
        params = {'did': did, 'siid': siid, 'piid': piid, 'value': value}
        for key, (handler, ctx) in list(self.properties.items()):
            if key in ((did, None, None), (did, siid, piid)):
                handler(dict(params), ctx)

    def event(self, did, siid=2, eiid=1):
        params = {'did': did, 'siid': siid, 'eiid': eiid, 'arguments': []}
        for key, (handler, ctx) in list(self.events.items()):
            if key in ((did, None, None), (did, siid, eiid)):
                handler(dict(params), ctx)


def load_client(source):
    tree = ast.parse(source)
    original_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'MIoTClient')
    names = {
        'sub_prop', 'unsub_prop', 'sub_event', 'remove_device_async',
        'get_prop_async', '__refresh_props_from_cloud',
        '__refresh_props_from_gw', '__refresh_props_from_lan',
        '__refresh_props_from_local',
        '__refresh_props_handler',
        '__cloud_property_overrides', '__sync_cloud_property_subscriptions',
        '__sub_from', '__unsub_from', '__update_device_msg_sub',
        '__on_mips_cloud_state_changed', '__on_default_prop_msg',
        '__on_prop_msg', '__on_event_msg', '__check_device_state',
    }
    methods = [copy.deepcopy(n) for n in original_class.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in names]
    for method in methods:
        method.decorator_list = []
    constants = [copy.deepcopy(n) for n in tree.body
                 if isinstance(n, ast.Assign) and any(
                     isinstance(t, ast.Name)
                     and t.id == 'CLOUD_PROPERTY_OVERRIDES'
                     for t in n.targets)]
    module = ast.Module(body=[
        ast.ImportFrom(
            module='__future__', names=[ast.alias(name='annotations')],
            level=0),
        *constants,
        ast.ClassDef(name='MIoTClient', bases=[], keywords=[], body=methods,
                     decorator_list=[]),
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        '_LOGGER': Mock(),
        'MIoTClientSub': types.SimpleNamespace,
        'MIoTClientError': RuntimeError,
        'CtrlMode': types.SimpleNamespace(AUTO='auto'),
        'MIoTDeviceState': types.SimpleNamespace(
            ONLINE='online', OFFLINE='offline'),
        'asyncio': asyncio,
        'traceback': traceback,
        'REFRESH_PROPS_DELAY': 0.2,
        'REFRESH_PROPS_RETRY_DELAY': 3,
    }
    # Load production method bodies without importing Home Assistant itself.
    exec(  # pylint: disable=exec-used
        compile(module, str(SOURCE_PATH), 'exec'), namespace)
    return namespace['MIoTClient']


@pytest.mark.github
class XiaomiHomePropertyRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_class = load_client(SOURCE_PATH.read_text(encoding='utf-8'))

    def client(self, *, model='xiaomi.lock.s20pro', gateway=True):
        client = self.client_class()
        client._device_list_cache = {'lock': {'model': model, 'online': True}}
        client._device_list_cloud = {'lock': {'online': True}}
        client._device_list_gateway = {'lock': {
            'online': gateway, 'push_available': gateway, 'group_id': 'hub'}}
        client._device_list_lan = {}
        client._sub_tree = TopicMatcher()
        client._sub_source_list = {}
        client._sub_device_state = {}
        client._ctrl_mode = 'auto'
        client._mips_cloud = Transport()
        client._mips_local = {'hub': Transport()}
        client._miot_lan = Transport()
        client._mips_local['hub'].get_prop_async = AsyncMock(return_value=13)
        client._miot_lan.get_prop_async = AsyncMock(return_value=13)
        client._miot_lan.init_done = True
        client._network = types.SimpleNamespace(network_status=True)
        client._http = types.SimpleNamespace(
            get_prop_async=AsyncMock(return_value=91),
            get_props_async=AsyncMock(return_value=[]))
        client._refresh_props_list = {}
        client._refresh_props_retry_count = 0
        client._refresh_props_timer = None
        client._main_loop = types.SimpleNamespace(
            call_later=Mock(return_value=Mock()))
        client._storage = types.SimpleNamespace(
            save_async=AsyncMock(return_value=True))
        client._uid, client._cloud_server = 'test', 'cn'
        client.request_refresh_prop = Mock()
        client._MIoTClient__request_refresh_cloud_devices = Mock()
        client._MIoTClient__request_show_devices_changed_notify = Mock()
        client._MIoTClient__on_cloud_device_state_changed = Mock()
        return client

    def subscribe(self, client, siid=9, piid=1):
        received = []
        client.sub_prop(
            'lock', lambda params, ctx: received.append(params['value']),
            siid=siid, piid=piid)
        return received

    def route(self, client):
        client._MIoTClient__update_device_msg_sub('lock')

    def connect(self, client):
        client._mips_cloud.mips_state = True
        asyncio.run(client._MIoTClient__on_mips_cloud_state_changed(
            'test-cn', True))

    def test_cloud_battery_survives_gateway_takeover(self):
        client = self.client(gateway=False)
        received = self.subscribe(client)
        self.route(client)
        client._mips_cloud.property('lock', 9, 1, 13)
        client._device_list_gateway['lock'].update(
            online=True, push_available=True)
        self.route(client)
        client._mips_cloud.property('lock', 9, 1, 91)
        self.assertEqual([13, 91], received)
        self.assertEqual('hub', client._sub_source_list['lock'])

    def test_precise_cloud_battery_rejects_stale_gateway_value(self):
        client = self.client()
        received = self.subscribe(client)
        self.route(client)
        client._mips_cloud.property('lock', 9, 1, 91)
        client._mips_local['hub'].property('lock', 9, 1, 13)
        self.assertEqual([91], received)
        self.assertEqual('hub', client._sub_source_list['lock'])

    def test_cloud_wildcard_and_precise_subscription_dispatch_once(self):
        client = self.client(gateway=False)
        received = self.subscribe(client)
        self.route(client)
        self.assertIn(('lock', None, None), client._mips_cloud.properties)
        self.assertIn(('lock', 9, 1), client._mips_cloud.properties)
        client._mips_cloud.property('lock', 9, 1, 91)
        self.assertEqual([91], received)

    def test_cloud_gateway_lan_route_changes_preserve_battery_source(self):
        client = self.client(gateway=False)
        received = self.subscribe(client)
        for expected, gateway, lan in (
                ('cloud', False, False), ('hub', True, False),
                ('lan', False, True), ('cloud', False, False)):
            with self.subTest(route=expected):
                client._device_list_gateway['lock'].update(
                    online=gateway, push_available=gateway)
                client._device_list_lan = {'lock': {
                    'online': lan, 'push_available': lan}}
                self.route(client)
                self.assertEqual(expected, client._sub_source_list['lock'])
                client._mips_cloud.property('lock', 9, 1, 91)
                client._mips_local['hub'].property('lock', 9, 1, 13)
                client._miot_lan.property('lock', 9, 1, 13)
        self.assertEqual([91, 91, 91, 91], received)

    def test_other_properties_and_other_models_keep_gateway_route(self):
        for model, siid, piid in (
                ('xiaomi.lock.s20pro', 9, 2), ('other.device', 9, 1)):
            with self.subTest(model=model, property=(siid, piid)):
                client = self.client(model=model)
                received = self.subscribe(client, siid, piid)
                self.route(client)
                client._mips_cloud.property('lock', siid, piid, 91)
                client._mips_local['hub'].property('lock', siid, piid, 13)
                self.assertEqual([13], received)
                self.assertNotIn(
                    ('lock', siid, piid), client._mips_cloud.properties)

    def test_events_keep_one_selected_source(self):
        client = self.client()
        self.subscribe(client)
        events = []
        client.sub_event(
            'lock', lambda params, ctx: events.append(params), siid=2, eiid=1)
        self.route(client)
        client._mips_cloud.event('lock')
        client._mips_local['hub'].event('lock')
        self.assertEqual(1, len(events))
        self.assertEqual({}, client._mips_cloud.events)

    def test_cloud_reconnect_restores_subscription_and_refreshes_once(self):
        client = self.client()
        received = self.subscribe(client)
        self.route(client)
        # disconnect() discards the transport matcher.
        client._mips_cloud.properties.clear()
        client._mips_cloud.mips_state = False
        self.connect(client)
        client.request_refresh_prop.assert_called_once_with(
            did='lock', siid=9, piid=1)
        client._mips_cloud.property('lock', 9, 1, 91)
        self.assertEqual([91], received)
        self.assertEqual('hub', client._sub_source_list['lock'])

    def test_unsubscribe_survives_cloud_reconnect(self):
        client = self.client()
        self.subscribe(client)
        other = self.subscribe(client, 9, 2)
        self.route(client)
        client.unsub_prop('lock', 9, 1)
        self.assertNotIn(('lock', 9, 1), client._mips_cloud.properties)
        self.connect(client)
        client.request_refresh_prop.assert_not_called()
        client._mips_local['hub'].property('lock', 9, 2, 100)
        self.assertEqual([100], other)

    def test_remove_cleans_override_consumer_without_resurrection(self):
        client = self.client()
        self.subscribe(client)
        self.subscribe(client, 9, 2)
        self.route(client)
        asyncio.run(client.remove_device_async('lock'))
        self.assertIsNone(client._sub_tree.get('lock/p/9/1'))
        self.assertIsNotNone(client._sub_tree.get('lock/p/9/2'))
        self.assertNotIn(('lock', 9, 1), client._mips_cloud.properties)
        self.connect(client)
        client.request_refresh_prop.assert_not_called()
        self.assertNotIn(('lock', 9, 1), client._mips_cloud.properties)

    def test_offline_registration_waits_for_cloud_connection(self):
        client = self.client()
        client._mips_cloud.mips_state = False
        received = self.subscribe(client)
        self.assertEqual({}, client._mips_cloud.properties)
        self.connect(client)
        client._mips_cloud.property('lock', 9, 1, 91)
        self.assertEqual([91], received)

    def test_offline_unsubscribe_and_remove_do_not_call_closed_cloud_loop(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                client = self.client()
                self.subscribe(client)
                self.route(client)
                client._mips_cloud.properties.clear()
                client._mips_cloud.mips_state = False
                if remove:
                    asyncio.run(client.remove_device_async('lock'))
                else:
                    client.unsub_prop('lock', 9, 1)
                self.connect(client)
                client.request_refresh_prop.assert_not_called()

    def test_http_refresh_still_delivers_authoritative_battery_value(self):
        client = self.client()
        received = self.subscribe(client)
        self.route(client)
        client._MIoTClient__on_prop_msg(
            {'did': 'lock', 'siid': 9, 'piid': 1, 'value': 91}, None)
        self.assertEqual([91], received)

    def test_wildcard_only_consumer_preserves_existing_route(self):
        client = self.client()
        received = []
        client.sub_prop(
            'lock', lambda params, ctx: received.append(params['value']))
        self.route(client)
        client._mips_local['hub'].property('lock', 9, 1, 13)
        self.assertEqual([13], received)

    def test_authoritative_getter_never_falls_back_to_old_local_battery(self):
        for cloud_result in (
                None, RuntimeError('cloud temporarily unavailable')):
            with self.subTest(cloud_result=cloud_result):
                client = self.client()
                client._device_list_gateway['lock']['specv2_access'] = True
                client._device_list_lan = {'lock': {'online': True}}
                if isinstance(cloud_result, Exception):
                    client._http.get_prop_async.side_effect = cloud_result
                else:
                    client._http.get_prop_async.return_value = cloud_result
                self.assertIsNone(asyncio.run(
                    client.get_prop_async('lock', 9, 1)))
                client._mips_local['hub'].get_prop_async.assert_not_awaited()
                client._miot_lan.get_prop_async.assert_not_awaited()

    def test_authoritative_getter_accepts_zero_and_offline_is_unknown(self):
        client = self.client()
        client._http.get_prop_async.return_value = 0
        self.assertEqual(0, asyncio.run(client.get_prop_async('lock', 9, 1)))
        client._network.network_status = False
        self.assertIsNone(asyncio.run(client.get_prop_async('lock', 9, 1)))
        client._http.get_prop_async.assert_awaited_once()
        client._mips_local['hub'].get_prop_async.assert_not_awaited()

    def test_cloud_values_preserve_zero_and_false_for_other_models(self):
        for model in ('xiaomi.lock.s20pro', 'other.device'):
            for value in (0, False):
                with self.subTest(model=model, value=value):
                    client = self.client(model=model)
                    client._http.get_prop_async.return_value = value
                    actual = asyncio.run(client.get_prop_async('lock', 9, 1))
                    self.assertEqual(value, actual)
                    self.assertIs(type(value), type(actual))
                    getter = client._mips_local['hub'].get_prop_async
                    getter.assert_not_awaited()

    def test_other_property_getter_keeps_gateway_fallback(self):
        client = self.client()
        client._http.get_prop_async.return_value = None
        client._device_list_gateway['lock']['specv2_access'] = True
        self.assertEqual(13, asyncio.run(client.get_prop_async('lock', 9, 2)))
        client._mips_local['hub'].get_prop_async.assert_awaited_once()

    def test_local_refresh_fallback_retains_cloud_only_property_for_retry(self):
        for route in ('gw', 'lan'):
            with self.subTest(route=route):
                client = self.client()
                received = self.subscribe(client)
                client._refresh_props_list = {'lock|9|1': {
                    'did': 'lock', 'siid': 9, 'piid': 1}}
                if route == 'lan':
                    client._mips_local = {}
                    client._device_list_lan = {'lock': {'online': True}}
                method = getattr(
                    client, f'_MIoTClient__refresh_props_from_{route}')
                self.assertFalse(asyncio.run(method()))
                self.assertIn('lock|9|1', client._refresh_props_list)
                self.assertEqual([], received)
                client._miot_lan.get_prop_async.assert_not_awaited()
                for gateway in client._mips_local.values():
                    gateway.get_prop_async.assert_not_awaited()

    def test_partial_cloud_reply_keeps_only_failed_item_and_can_recover(self):
        client = self.client()
        battery = self.subscribe(client)
        backup = self.subscribe(client, 9, 2)
        client._refresh_props_list = {
            f'lock|9|{piid}': {'did': 'lock', 'siid': 9, 'piid': piid}
            for piid in (1, 2)}
        client._http.get_props_async.side_effect = [
            [{'did': 'lock', 'siid': 9, 'piid': 1, 'code': -704042011},
             {'did': 'lock', 'siid': 9, 'piid': 2, 'value': 100, 'code': 0}],
            [{'did': 'lock', 'siid': 9, 'piid': 1, 'value': 91, 'code': 0}],
        ]
        refresh = client._MIoTClient__refresh_props_from_cloud
        self.assertFalse(asyncio.run(refresh()))
        self.assertEqual(['lock|9|1'], list(client._refresh_props_list))
        self.assertEqual([], battery)
        self.assertEqual([100], backup)
        self.assertTrue(asyncio.run(refresh()))
        self.assertEqual({}, client._refresh_props_list)
        self.assertEqual([91], battery)

    def test_missing_cloud_reply_stops_at_existing_retry_budget(self):
        client = self.client()
        received = self.subscribe(client)
        client._refresh_props_list = {'lock|9|1': {
            'did': 'lock', 'siid': 9, 'piid': 1}}
        client._http.get_props_async.return_value = [
            {'did': 'lock', 'siid': 9, 'piid': 1, 'code': -704042011}]
        for attempt in range(4):
            asyncio.run(client._MIoTClient__refresh_props_handler())
            self.assertEqual(attempt < 3, bool(client._refresh_props_list))
        self.assertEqual(4, client._http.get_props_async.await_count)
        self.assertEqual(0, client._refresh_props_retry_count)
        self.assertIsNone(client._refresh_props_timer)
        self.assertEqual(3, client._main_loop.call_later.call_count)
        client._mips_local['hub'].get_prop_async.assert_not_awaited()
        self.assertEqual([], received)
