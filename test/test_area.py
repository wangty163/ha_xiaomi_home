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


class FakeAreaRegistry:
    """In-memory area registry test double."""

    def __init__(self, areas):
        self.areas = {area.name: area for area in areas}

    def async_get_area_by_name(self, name):
        return self.areas.get(name)

    def async_get_or_create(self, name):
        area = FakeArea(id=f'area-{len(self.areas) + 1}', name=name)
        self.areas[name] = area
        return area


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
    assert result.missing_devices == 1
    assert devices.updates == [('device-move', 'bedroom')]


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
