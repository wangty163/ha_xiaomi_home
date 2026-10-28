# -*- coding: utf-8 -*-
"""Exercise playback freshness using the released media player source."""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.github

SOURCE = (Path(__file__).resolve().parents[1]
          / 'custom_components/xiaomi_home/media_player.py')
ACTION_FEATURES = (
    ('FeaturePlay', 'play', 'async_media_play'),
    ('FeaturePause', 'pause', 'async_media_pause'),
    ('FeatureStop', 'stop', 'async_media_stop'),
    ('FeatureNextTrack', 'next', 'async_media_next_track'),
    ('FeaturePreviousTrack', 'previous', 'async_media_previous_track'),
)


class Property:
    """Hashable MIoT playing-state property."""

    name = 'playing-state'
    value_list = SimpleNamespace(items=[
        SimpleNamespace(name='stop', value=0),
        SimpleNamespace(name='playing', value=1),
        SimpleNamespace(name='pause', value=2),
    ])


class NativeEntity:
    """Only the native entity interfaces used by these feature classes."""

    def __init__(self, miot_device, entity_data):
        del miot_device
        self.entity_data = entity_data
        self.entity_id = 'media_player.test_speaker'
        self._attr_supported_features = 0
        self.values = {}
        self.change_handlers = {}
        self.state_writes = []
        self.added_count = 0
        self.read = AsyncMock(return_value=1)
        self.action_async = AsyncMock()

    def get_prop_value(self, prop):
        return self.values.get(prop)

    def set_prop_value(self, prop, value):
        self.values[prop] = value

    def sub_prop_changed(self, prop, handler):
        self.change_handlers[prop] = handler

    async def get_property_async(self, prop):
        # Match the native helper's existing-map-key requirement. An entity
        # must be readable even when startup has not received a push yet.
        previous = self.values[prop]
        value = await self.read(prop)
        if value != previous:
            self.values[prop] = value
        return value

    def async_write_ha_state(self):
        self.state_writes.append(self.state)

    async def async_added_to_hass(self):
        self.added_count += 1

    @staticmethod
    def get_map_value(map_, key):
        return map_.get(key)

    @property
    def extra_state_attributes(self):
        return {'native_attribute': 'preserved'}


class MediaEntity:
    """Separate media base preserves the production cooperative MRO."""


@pytest.fixture(name='player')
def fixture_player():
    """Load production feature classes without importing Home Assistant."""
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    names = {'FeatureState', *(entry[0] for entry in ACTION_FEATURES)}
    classes = [node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name in names]
    module = ast.Module(body=classes, type_ignores=[])
    ast.fix_missing_locations(module)
    logger = Mock()
    namespace = {
        'MIoTServiceEntity': NativeEntity,
        'MediaPlayerEntity': MediaEntity,
        'MIoTDevice': object,
        'MIoTEntityData': object,
        'MIoTSpecProperty': Property,
        'MIoTSpecAction': object,
        'MediaPlayerEntityFeature': SimpleNamespace(
            PLAY=1, PAUSE=2, STOP=4, NEXT_TRACK=8, PREVIOUS_TRACK=16),
        'MediaPlayerState': SimpleNamespace(
            OFF='off', IDLE='idle', PLAYING='playing', PAUSED='paused'),
        'Optional': Optional,
        'datetime': datetime,
        'timezone': timezone,
        '_LOGGER': logger,
    }
    # Compile the exact released classes; the native networking boundary is
    # the only test double and no cached/live integration source is used.
    exec(compile(module, str(SOURCE), 'exec'), namespace)  # pylint: disable=exec-used
    bases = tuple(namespace[entry[0]] for entry in ACTION_FEATURES)
    player_class = type('Player', (*bases, namespace['FeatureState']), {})
    prop = Property()
    entity = player_class(None, SimpleNamespace(
        props=[prop], actions=[SimpleNamespace(name=entry[1])
                              for entry in ACTION_FEATURES]))
    return SimpleNamespace(entity=entity, prop=prop, logger=logger)


@pytest.mark.parametrize('value, expected', [
    (0, 'idle'), (1, 'playing'), (2, 'paused')])
def test_forced_read_preserves_native_values(player, value, expected):
    entity = player.entity
    entity.read.return_value = value
    assert entity.state is None
    assert not entity.extra_state_attributes['playback_state_fresh']

    asyncio.run(entity.async_update())

    entity.read.assert_awaited_once_with(player.prop)
    assert entity.state == expected
    assert entity.extra_state_attributes['playback_state_fresh']
    timestamp = entity.extra_state_attributes['playback_state_refreshed_at']
    assert datetime.fromisoformat(timestamp).utcoffset().total_seconds() == 0
    assert entity.extra_state_attributes['native_attribute'] == 'preserved'
    assert entity.state_writes[-1] == expected
    entity.action_async.assert_not_awaited()


@pytest.mark.parametrize('failure', [None, RuntimeError('read failed')])
def test_failed_read_invalidates_old_state_before_network(player, failure):
    entity = player.entity
    asyncio.run(entity.async_update())
    assert entity.state == 'playing'

    async def fail_read(prop):
        assert prop is player.prop
        assert entity.state is None
        assert not entity.extra_state_attributes['playback_state_fresh']
        assert entity.state_writes[-1] is None
        if isinstance(failure, Exception):
            raise failure
        return failure

    entity.read.side_effect = fail_read
    with pytest.raises(RuntimeError):
        asyncio.run(entity.async_update())

    assert entity.state is None
    assert not entity.extra_state_attributes['playback_state_fresh']
    entity.action_async.assert_not_awaited()


@pytest.mark.parametrize('feature, action, method', ACTION_FEATURES)
def test_transport_action_invalidates_then_sends_once(
        player, feature, action, method):
    del feature, action
    entity = player.entity
    asyncio.run(entity.async_update())
    entity.read.reset_mock()

    async def send_action(**_kwargs):
        assert entity.state is None
        assert not entity.extra_state_attributes['playback_state_fresh']

    entity.action_async.side_effect = send_action
    asyncio.run(getattr(entity, method)())

    entity.action_async.assert_awaited_once()
    entity.read.assert_not_awaited()
    assert entity.state is None


def test_push_restores_fresh_state_without_network_retry(player):
    entity = player.entity
    asyncio.run(entity.async_update())
    asyncio.run(entity.async_media_pause())
    entity.read.reset_mock()

    entity.values[player.prop] = 2
    entity.change_handlers[player.prop](player.prop, 2)

    assert entity.state == 'paused'
    assert entity.extra_state_attributes['playback_state_fresh']
    entity.read.assert_not_awaited()
    entity.action_async.assert_awaited_once()


def test_startup_registration_precedes_initial_read(player):
    entity = player.entity

    async def initial_read(prop):
        assert prop is player.prop
        assert entity.added_count == 1
        return 0

    entity.read.side_effect = initial_read
    asyncio.run(entity.async_added_to_hass())

    assert entity.state == 'idle'
    assert entity.extra_state_attributes['playback_state_fresh']
    entity.read.assert_awaited_once_with(player.prop)
    player.logger.warning.assert_not_called()
    entity.action_async.assert_not_awaited()


def test_startup_failure_keeps_entity_registered_and_unknown(player):
    entity = player.entity
    entity.read.side_effect = RuntimeError('device unavailable')

    asyncio.run(entity.async_added_to_hass())

    assert entity.added_count == 1
    assert entity.state is None
    assert not entity.extra_state_attributes['playback_state_fresh']
    player.logger.warning.assert_called_once()
    entity.action_async.assert_not_awaited()
