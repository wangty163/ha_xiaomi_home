# -*- coding: utf-8 -*-
"""Unit tests for Xiaomi Home device area synchronization."""
from dataclasses import dataclass

import pytest

# pylint: disable=import-outside-toplevel


@dataclass
class FakeArea:
    """Minimal area registry entry."""

    id: str
    name: str


@dataclass
class FakeDevice:
    """Minimal device registry entry."""

    id: str
    area_id: str | None
    name: str | None = None


@dataclass
class FakeEntity:
    """Minimal entity registry entry."""

    area_id: str | None


class FakeAreaRegistry:
    """In-memory area registry test double."""

    def __init__(self, areas):
        self.areas = {area.name: area for area in areas}

    def async_get_area_by_name(self, name):
        return self.areas.get(name)

    def async_get_area(self, area_id):
        return next(
            (area for area in self.areas.values() if area.id == area_id),
            None)

    def async_get_or_create(self, name):
        area = FakeArea(id=f'area-{len(self.areas) + 1}', name=name)
        self.areas[name] = area
        return area

    def async_delete(self, area_id):
        area = self.async_get_area(area_id)
        if area:
            self.areas.pop(area.name)


class FakeDeviceRegistry:
    """In-memory device registry test double."""

    def __init__(self, devices):
        self.devices = devices
        self.updates = []

    def async_get_device(self, identifiers, connections):
        del connections
        _, identifier = next(iter(identifiers))
        return self.devices.get(identifier)

    def async_update_device(self, device_id, area_id):
        device = next(
            item for item in self.devices.values() if item.id == device_id)
        device.area_id = area_id
        self.updates.append((device_id, area_id))


class FakeEntityRegistry:
    """In-memory entity registry test double."""

    def __init__(self, entities):
        self.entities = entities


@pytest.mark.github
def test_sync_device_area_entries_updates_only_mismatches():
    from miot.area import sync_device_area_entries

    areas = FakeAreaRegistry([
        FakeArea(id='living-room', name='Living Room'),
        FakeArea(id='bedroom', name='Bedroom'),
    ])
    devices = FakeDeviceRegistry({
        'cn_match': FakeDevice(id='device-match', area_id='living-room'),
        'cn_move': FakeDevice(id='device-move', area_id='living-room'),
    })

    result = sync_device_area_entries(
        area_registry=areas,
        device_registry=devices,
        device_area_map={
            'cn_match': 'Living Room',
            'cn_move': 'Bedroom',
            'cn_missing': 'Living Room',
        },
        domain='xiaomi_home',
    )

    assert result.scanned == 3
    assert result.matched == 1
    assert result.updated == 1
    assert result.created_areas == 0
    assert result.deleted_areas == 0
    assert result.missing_devices == 1
    assert devices.updates == [('device-move', 'bedroom')]
    assert result.device_changes[0].old_area_name == 'Living Room'
    assert result.device_changes[0].new_area_name == 'Bedroom'


@pytest.mark.github
def test_sync_device_area_entries_reuses_new_area():
    from miot.area import sync_device_area_entries

    areas = FakeAreaRegistry([])
    devices = FakeDeviceRegistry({
        'cn_first': FakeDevice(id='device-first', area_id=None),
        'cn_second': FakeDevice(id='device-second', area_id=None),
    })

    result = sync_device_area_entries(
        area_registry=areas,
        device_registry=devices,
        device_area_map={
            'cn_first': 'New Room',
            'cn_second': 'New Room',
        },
        domain='xiaomi_home',
    )

    assert result.updated == 2
    assert result.created_areas == 1
    assert len(areas.areas) == 1
    assert result.created_area_names == ('New Room',)
    assert dict(result.managed_areas) == {'area-1': 'New Room'}


@pytest.mark.github
def test_sync_device_area_entries_deletes_only_unused_managed_area():
    from miot.area import sync_device_area_entries

    areas = FakeAreaRegistry([
        FakeArea(id='old-room', name='Old Room'),
        FakeArea(id='user-room', name='User Room'),
    ])
    devices = FakeDeviceRegistry({
        'cn_move': FakeDevice(
            id='device-move', area_id='old-room', name='Air Purifier'),
    })

    result = sync_device_area_entries(
        area_registry=areas,
        device_registry=devices,
        entity_registry=FakeEntityRegistry({}),
        device_area_map={'cn_move': 'New Room'},
        domain='xiaomi_home',
        managed_areas={'old-room': 'Old Room'},
    )

    assert result.updated == 1
    assert result.created_area_names == ('New Room',)
    assert result.deleted_area_names == ('Old Room',)
    assert result.deleted_areas == 1
    assert 'Old Room' not in areas.areas
    assert 'User Room' in areas.areas
    assert dict(result.managed_areas) == {'area-3': 'New Room'}


@pytest.mark.github
def test_sync_device_area_entries_relinquishes_occupied_stale_area():
    from miot.area import sync_device_area_entries

    areas = FakeAreaRegistry([
        FakeArea(id='managed-room', name='Managed Room'),
        FakeArea(id='target-room', name='Target Room'),
    ])
    devices = FakeDeviceRegistry({
        'cn_match': FakeDevice(id='device-match', area_id='target-room'),
    })

    result = sync_device_area_entries(
        area_registry=areas,
        device_registry=devices,
        entity_registry=FakeEntityRegistry({
            'light.user_light': FakeEntity(area_id='managed-room'),
        }),
        device_area_map={'cn_match': 'Target Room'},
        domain='xiaomi_home',
        managed_areas={'managed-room': 'Managed Room'},
    )

    assert result.deleted_areas == 0
    assert 'Managed Room' in areas.areas
    assert result.managed_areas == ()


@pytest.mark.github
def test_sync_device_area_entries_does_not_clean_up_from_empty_cloud_data():
    from miot.area import sync_device_area_entries

    areas = FakeAreaRegistry([
        FakeArea(id='managed-room', name='Managed Room'),
    ])

    result = sync_device_area_entries(
        area_registry=areas,
        device_registry=FakeDeviceRegistry({}),
        entity_registry=FakeEntityRegistry({}),
        device_area_map={},
        domain='xiaomi_home',
        managed_areas={'managed-room': 'Managed Room'},
    )

    assert result.deleted_areas == 0
    assert 'Managed Room' in areas.areas
    assert dict(result.managed_areas) == {
        'managed-room': 'Managed Room'}


@pytest.mark.github
def test_sync_device_area_entries_never_deletes_renamed_managed_area():
    from miot.area import sync_device_area_entries

    areas = FakeAreaRegistry([
        FakeArea(id='managed-room', name='User Renamed Room'),
        FakeArea(id='target-room', name='Target Room'),
    ])

    result = sync_device_area_entries(
        area_registry=areas,
        device_registry=FakeDeviceRegistry({
            'cn_match': FakeDevice(
                id='device-match', area_id='target-room'),
        }),
        entity_registry=FakeEntityRegistry({}),
        device_area_map={'cn_match': 'Target Room'},
        domain='xiaomi_home',
        managed_areas={'managed-room': 'Managed Room'},
    )

    assert result.deleted_areas == 0
    assert 'User Renamed Room' in areas.areas
    assert result.managed_areas == ()


@pytest.mark.github
def test_area_sync_is_enabled_keeps_legacy_behavior():
    from miot.area import area_sync_is_enabled

    assert area_sync_is_enabled({'area_name_rule': 'room'}) is True
    assert area_sync_is_enabled({'area_name_rule': 'none'}) is False
    assert area_sync_is_enabled({
        'area_name_rule': 'room', 'area_sync_enabled': False}) is False


@pytest.mark.github
def test_area_sync_rule_options_excludes_legacy_off_value():
    from miot.area import area_sync_rule_options

    assert area_sync_rule_options({
        'none': '不同步',
        'home_room': '家庭名 和 房间名',
        'room': '房间名',
        'home': '家庭名',
    }) == {
        'home_room': '家庭名 和 房间名',
        'room': '房间名',
        'home': '家庭名',
    }


@pytest.mark.github
def test_format_area_sync_notification_in_chinese():
    from miot.area import (
        AreaSyncResult, DeviceAreaChange, format_area_sync_notification)

    notification = format_area_sync_notification(
        result=AreaSyncResult(
            updated=1,
            created_areas=1,
            deleted_areas=1,
            device_changes=(DeviceAreaChange(
                device_name='空气净化器',
                old_area_name='客厅',
                new_area_name='书房'),),
            created_area_names=('书房',),
            deleted_area_names=('旧房间',),
        ),
        language='zh-Hans',
    )

    assert notification is not None
    title, message = notification
    assert title == 'Xiaomi Home 房间名称和设备区域同步'
    assert '空气净化器：客厅 → 书房' in message
    assert '**新增区域（1）**' in message
    assert '**删除区域（1）**' in message


@pytest.mark.github
def test_format_area_sync_notification_skips_noop():
    from miot.area import AreaSyncResult, format_area_sync_notification

    assert format_area_sync_notification(
        AreaSyncResult(matched=3), 'en') is None


@pytest.mark.github
def test_area_sync_control_labels_describe_both_effects():
    """Keep internal option keys from leaking into the configuration UI."""
    import json
    from pathlib import Path

    translation_dir = (
        Path(__file__).resolve().parents[1]
        / 'custom_components/xiaomi_home/translations')
    expected = {
        'en': (
            'Synchronize room names and device areas',
            'Synchronization logic'),
        'zh-Hans': (
            '同步房间名称和设备区域',
            '同步逻辑'),
    }

    for language, labels in expected.items():
        translation = json.loads(
            (translation_dir / f'{language}.json').read_text(
                encoding='utf-8'))
        setup_data = translation['config']['step']['homes_select']['data']
        options_data = translation['options']['step'][
            'config_options']['data']
        setup_rule_data = translation['config']['step'][
            'area_sync_rule']['data']
        options_rule_data = translation['options']['step'][
            'area_sync_rule']['data']
        assert setup_data['area_sync_enabled'] == labels[0]
        assert options_data['area_sync_enabled'] == labels[0]
        assert setup_rule_data['area_sync_rule'] == labels[1]
        assert options_rule_data['area_sync_rule'] == labels[1]
        assert 'area_name_rule' not in setup_rule_data
        assert 'area_name_rule' not in options_rule_data
        assert 'area_name_rule' not in setup_data
        assert 'area_name_rule' not in options_data
