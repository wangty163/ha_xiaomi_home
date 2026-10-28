# -*- coding: utf-8 -*-
"""Replay device-state changes between construction and HA registration."""
from __future__ import annotations

from abc import abstractmethod
import ast
import asyncio
import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# These private fields and callbacks are the production state machine.
# pylint: disable=protected-access

pytestmark = pytest.mark.github
SOURCE = (Path(__file__).resolve().parents[1]
          / 'custom_components/xiaomi_home/miot/miot_device.py')
ENTITY_CLASSES = (
    'MIoTServiceEntity', 'MIoTPropertyEntity',
    'MIoTEventEntity', 'MIoTActionEntity')
# AST fingerprints of the exact v0.5.4 registration methods. Removing only
# the new snapshot must reproduce these methods, not a handwritten model.
ORIGINAL_ADDED_HASHES = {
    'MIoTServiceEntity':
        'be05fcc2202f3b238e42a9beffea21b055770f83defd0b96673840aa9f8c87ed',
    'MIoTPropertyEntity':
        'ae2e9fa5870557a4a968d7c86d5fa2cb70c5db4bd48967ce95b09576d1a26503',
    'MIoTEventEntity':
        'ff7a506742cfcc824c1afacfa2b82904345e6239d2a3307186f978bf6da62d00',
    'MIoTActionEntity':
        'cab643add679ac5d6047dab9f6cafe93ac2b4d581641be726708b6807a2d422e',
}


class EntityBoundary:
    """HA writes are recorded without controlling a device."""

    def async_write_ha_state(self):
        self.writes.append(self._attr_available)


class ServiceSpec(SimpleNamespace):
    """A distinguishable MIoT service type for production isinstance checks."""


class InstanceSpec:
    """An alternate MIoT instance type."""


class Spec(SimpleNamespace):
    """Hashable property, event, or action metadata."""

    __hash__ = object.__hash__


class LoopBoundary:
    """Queue native state broadcasts and preserve their original ordering."""

    def __init__(self):
        self.pending = []
        self.timers = []

    def call_soon_threadsafe(self, handler, *args):
        self.pending.append((handler, args))

    def call_later(self, delay, handler):
        timer = Mock(delay=delay, handler=handler)
        self.timers.append(timer)
        return timer

    def drain(self):
        while self.pending:
            handler, args = self.pending.pop(0)
            handler(*args)


def load_classes(original=False):
    """Load actual constructors, subscriptions, updates, and removal logic."""
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    classes = [copy.deepcopy(node) for node in tree.body
               if isinstance(node, ast.ClassDef)
               and node.name in {'MIoTDevice', *ENTITY_CLASSES}]
    if original:
        snapshot = ast.dump(ast.parse(
            'self._attr_available = self.miot_device.online').body[0])
        for class_node in classes:
            if class_node.name not in ENTITY_CLASSES:
                continue
            added = next(method for method in class_node.body
                         if isinstance(method, ast.AsyncFunctionDef)
                         and method.name == 'async_added_to_hass')
            previous_count = len(added.body)
            added.body = [node for node in added.body
                          if ast.dump(node) != snapshot]
            assert len(added.body) == previous_count - 1
            digest = hashlib.sha256(ast.dump(added).encode()).hexdigest()
            assert digest == ORIGINAL_ADDED_HASHES[class_node.name]
    module = ast.Module(body=[
        ast.ImportFrom(module='__future__', names=[
            ast.alias(name='annotations')], level=0),
        *classes,
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        'Entity': EntityBoundary, 'abstractmethod': abstractmethod,
        'MIoTSpecInstance': InstanceSpec, 'MIoTSpecService': ServiceSpec,
        'DOMAIN': 'xiaomi_home', 'MIoTDeviceError': RuntimeError,
        'MIoTDeviceState': SimpleNamespace(ONLINE='online', OFFLINE='offline'),
        'ha_entity_domain': lambda entity: 'sensor',
        'to_valid_entity_id': lambda entity_id: entity_id,
        'slugify_name': lambda name: name,
        'slugify_did': lambda cloud_server, did: f'{cloud_server}_{did}',
        '_LOGGER': Mock(),
    }
    exec(compile(module, str(SOURCE), 'exec'), namespace)  # pylint: disable=exec-used
    return namespace


def make_entity(class_name, online, original=False):
    """Keep the device's real subscription state machine with fake I/O."""
    namespace = load_classes(original=original)
    loop = LoopBoundary()
    client = Mock(main_loop=loop, cloud_server='cn')
    device = namespace['MIoTDevice'].__new__(namespace['MIoTDevice'])
    device.miot_client = client
    device._online = online
    device._did = 'speaker'
    device._name = 'Test speaker'
    device._model_strs = ['xiaomi', 'speaker', 'test']
    device._sub_id = 0
    device._device_state_sub_list = {}
    device._value_sub_list = {}
    service = ServiceSpec(
        iid=3, name='speaker', description='speaker',
        description_trans='Speaker', proprietary=False, entity_category=None)
    spec = Spec(
        iid=1, name='playing-state', description_trans='State',
        service=service, platform='sensor', device_class=None,
        value_range=None, value_list=None, readable=True, notifiable=True,
        proprietary=False, argument=[])
    if class_name == 'MIoTServiceEntity':
        entity = namespace[class_name](device, SimpleNamespace(
            spec=service, props=[spec], events=[], entity_category=None))
    else:
        entity = namespace[class_name](device, spec)
    entity.writes = []
    return SimpleNamespace(
        entity=entity, device=device, loop=loop, client=client)


def set_online(case, online):
    """Deliver the actual device transition, including subscriber dispatch."""
    state = 'online' if online else 'offline'
    case.device._MIoTDevice__on_device_state_changed('speaker', state, None)
    case.loop.drain()


@pytest.mark.parametrize('class_name', ENTITY_CLASSES)
@pytest.mark.parametrize('initial', [False, True])
def test_original_registration_gap_retains_wrong_constructor_state(
        class_name, initial):
    case = make_entity(class_name, initial, original=True)
    set_online(case, not initial)

    asyncio.run(case.entity.async_added_to_hass())
    case.loop.drain()

    assert case.device.online is not initial
    assert case.entity._attr_available is initial


@pytest.mark.parametrize('class_name', ENTITY_CLASSES)
@pytest.mark.parametrize('initial,current', [
    (False, True), (True, False), (False, False), (True, True)])
def test_registration_uses_current_online_state_without_queued_replay(
        class_name, initial, current):
    case = make_entity(class_name, initial)
    set_online(case, current)

    asyncio.run(case.entity.async_added_to_hass())

    assert case.device.online is current
    assert case.entity._attr_available is current
    assert not case.loop.pending
    expected_reads = int(current and class_name in {
        'MIoTServiceEntity', 'MIoTPropertyEntity'})
    assert case.client.request_refresh_prop.call_count == expected_reads
    case.client.action_async.assert_not_called()
    case.client.set_prop_async.assert_not_called()


@pytest.mark.parametrize('class_name', ENTITY_CLASSES)
def test_future_transitions_and_unsubscribe_keep_native_behavior(class_name):
    case = make_entity(class_name, False)
    asyncio.run(case.entity.async_added_to_hass())
    for online in (True, False, True):
        set_online(case, online)
        assert case.entity._attr_available is online
    case.client.request_refresh_prop.reset_mock()

    asyncio.run(case.entity.async_will_remove_from_hass())
    case.entity.writes.clear()
    assert not case.device._device_state_sub_list
    assert not case.device._value_sub_list
    set_online(case, False)

    assert not case.device.online
    assert case.entity._attr_available
    assert not case.entity.writes
    assert not case.loop.pending
    case.client.request_refresh_prop.assert_not_called()
    case.client.action_async.assert_not_called()
    case.client.set_prop_async.assert_not_called()
