# -*- coding: utf-8 -*-
"""Synchronize Xiaomi Home device rooms with Home Assistant areas."""
from dataclasses import dataclass
from typing import Any, Optional


AREA_SYNC_RULES = {'home', 'room', 'home_room'}
CONF_AREA_SYNC_ENABLED = 'area_sync_enabled'
CONF_AREA_SYNC_MANAGED_AREAS = 'area_sync_managed_areas'


@dataclass(frozen=True)
class DeviceAreaChange:
    """A device area changed by an area synchronization run."""

    device_name: str
    old_area_name: Optional[str]
    new_area_name: str


@dataclass(frozen=True)
class AreaSyncResult:
    """Summary of a device area synchronization run."""

    scanned: int = 0
    matched: int = 0
    updated: int = 0
    created_areas: int = 0
    deleted_areas: int = 0
    missing_devices: int = 0
    device_changes: tuple[DeviceAreaChange, ...] = ()
    created_area_names: tuple[str, ...] = ()
    deleted_area_names: tuple[str, ...] = ()
    managed_areas: tuple[tuple[str, str], ...] = ()


def area_sync_is_enabled(entry_data: dict) -> bool:
    """Return whether area sync is enabled, including legacy entries."""
    if CONF_AREA_SYNC_ENABLED in entry_data:
        return bool(entry_data[CONF_AREA_SYNC_ENABLED])
    return entry_data.get('area_name_rule') in AREA_SYNC_RULES


def area_sync_rule_options(
    translated_options: dict[str, str]
) -> dict[str, str]:
    """Return actual synchronization rules, excluding the legacy off value."""
    return {
        rule: label
        for rule, label in translated_options.items()
        if rule in AREA_SYNC_RULES
    }


def _area_name_by_id(area_registry: Any, area_id: Optional[str]) -> None | str:
    if not area_id:
        return None
    area_entry = area_registry.async_get_area(area_id)
    return area_entry.name if area_entry else None


def _area_is_occupied(
    area_id: str, device_registry: Any, entity_registry: Any
) -> bool:
    devices = getattr(device_registry, 'devices', {})
    if any(entry.area_id == area_id for entry in devices.values()):
        return True
    if entity_registry is None:
        return False
    entities = getattr(entity_registry, 'entities', {})
    return any(entry.area_id == area_id for entry in entities.values())


def sync_device_area_entries(
    area_registry: Any,
    device_registry: Any,
    device_area_map: dict[str, str],
    domain: str,
    entity_registry: Any = None,
    managed_areas: Optional[dict[str, str]] = None,
) -> AreaSyncResult:
    """Apply target areas to existing Home Assistant device entries.

    Only areas previously created by this function are eligible for deletion.
    A managed area is deleted only after it is no longer referenced by Xiaomi
    Home and has no device or entity assigned to it.
    """
    matched = 0
    missing_devices = 0
    device_changes: list[DeviceAreaChange] = []
    created_area_names: list[str] = []
    deleted_area_names: list[str] = []
    managed = dict(managed_areas or {})
    desired_area_names = set(device_area_map.values())

    for identifier, area_name in device_area_map.items():
        device_entry = device_registry.async_get_device(
            identifiers={(domain, identifier)}, connections=None)
        if device_entry is None:
            missing_devices += 1
            continue

        area_entry = area_registry.async_get_area_by_name(area_name)
        if area_entry is None:
            area_entry = area_registry.async_get_or_create(area_name)
            created_area_names.append(area_name)
            managed[area_entry.id] = area_name

        if device_entry.area_id == area_entry.id:
            matched += 1
            continue

        device_changes.append(DeviceAreaChange(
            device_name=(
                getattr(device_entry, 'name_by_user', None)
                or getattr(device_entry, 'name', None)
                or identifier),
            old_area_name=_area_name_by_id(
                area_registry, device_entry.area_id),
            new_area_name=area_name,
        ))
        device_registry.async_update_device(
            device_id=device_entry.id, area_id=area_entry.id)

    # Never perform area cleanup from an empty cloud result. This avoids
    # deleting managed areas during a transient metadata failure.
    if device_area_map:
        for area_id, area_name in tuple(managed.items()):
            area_entry = area_registry.async_get_area(area_id)
            if area_entry is None:
                managed.pop(area_id, None)
                if area_registry.async_get_area_by_name(area_name) is None:
                    deleted_area_names.append(area_name)
                continue
            if area_entry.name != area_name:
                # A rename is a user action. Relinquish ownership of that area.
                managed.pop(area_id, None)
                continue
            if area_name in desired_area_names:
                continue
            if _area_is_occupied(
                    area_id, device_registry, entity_registry):
                # Once a user assigns something to a stale managed area, stop
                # owning it so it can never be deleted later by this feature.
                managed.pop(area_id, None)
                continue
            area_registry.async_delete(area_id)
            managed.pop(area_id, None)
            deleted_area_names.append(area_name)

    return AreaSyncResult(
        scanned=len(device_area_map),
        matched=matched,
        updated=len(device_changes),
        created_areas=len(created_area_names),
        deleted_areas=len(deleted_area_names),
        missing_devices=missing_devices,
        device_changes=tuple(device_changes),
        created_area_names=tuple(created_area_names),
        deleted_area_names=tuple(deleted_area_names),
        managed_areas=tuple(sorted(managed.items())),
    )


def format_area_sync_notification(
    result: AreaSyncResult, language: str
) -> None | tuple[str, str]:
    """Format a persistent notification for material sync changes."""
    if not (
        result.device_changes
        or result.created_area_names
        or result.deleted_area_names
    ):
        return None

    is_chinese = language.lower().startswith('zh')
    if is_chinese:
        title = 'Xiaomi Home 房间名称和设备区域同步'
        lines = ['已完成房间名称和设备区域同步：']
        if result.device_changes:
            lines.append(f'\n**自动调整设备区域（{result.updated}）**')
            for change in result.device_changes:
                old_name = change.old_area_name or '未分配区域'
                lines.append(
                    f'- {change.device_name}：{old_name} → '
                    f'{change.new_area_name}')
        if result.created_area_names:
            lines.append(f'\n**新增区域（{result.created_areas}）**')
            lines.extend(f'- {name}' for name in result.created_area_names)
        if result.deleted_area_names:
            lines.append(f'\n**删除区域（{result.deleted_areas}）**')
            lines.extend(f'- {name}' for name in result.deleted_area_names)
        return title, '\n'.join(lines)

    title = 'Xiaomi Home room name and device area synchronization'
    lines = ['Room name and device area synchronization completed:']
    if result.device_changes:
        lines.append(f'\n**Devices moved ({result.updated})**')
        for change in result.device_changes:
            old_name = change.old_area_name or 'No area'
            lines.append(
                f'- {change.device_name}: {old_name} → '
                f'{change.new_area_name}')
    if result.created_area_names:
        lines.append(f'\n**Areas created ({result.created_areas})**')
        lines.extend(f'- {name}' for name in result.created_area_names)
    if result.deleted_area_names:
        lines.append(f'\n**Areas deleted ({result.deleted_areas})**')
        lines.extend(f'- {name}' for name in result.deleted_area_names)
    return title, '\n'.join(lines)
