# -*- coding: utf-8 -*-
"""Synchronize Xiaomi Home device rooms with Home Assistant areas."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AreaSyncResult:
    """Summary of a device area synchronization run."""

    scanned: int = 0
    matched: int = 0
    updated: int = 0
    created_areas: int = 0
    missing_devices: int = 0


def sync_device_area_entries(
    area_registry: Any,
    device_registry: Any,
    device_area_map: dict[str, str],
    domain: str,
) -> AreaSyncResult:
    """Apply target areas to existing Home Assistant device entries."""
    matched = 0
    updated = 0
    created_areas = 0
    missing_devices = 0

    for identifier, area_name in device_area_map.items():
        device_entry = device_registry.async_get_device(
            identifiers={(domain, identifier)}, connections=None)
        if device_entry is None:
            missing_devices += 1
            continue

        area_entry = area_registry.async_get_area_by_name(area_name)
        if area_entry is None:
            area_entry = area_registry.async_get_or_create(area_name)
            created_areas += 1

        if device_entry.area_id == area_entry.id:
            matched += 1
            continue

        device_registry.async_update_device(
            device_id=device_entry.id, area_id=area_entry.id)
        updated += 1

    return AreaSyncResult(
        scanned=len(device_area_map),
        matched=matched,
        updated=updated,
        created_areas=created_areas,
        missing_devices=missing_devices,
    )
