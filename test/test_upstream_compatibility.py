# -*- coding: utf-8 -*-
"""Protect upstream entity migration and localized configuration contracts."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.github

COMPONENT = (Path(__file__).resolve().parents[1]
             / 'custom_components/xiaomi_home')


def load_functions(path, names, namespace):
    """Compile exact production functions without importing Home Assistant."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    functions = [node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == set(names)
    module = ast.Module(body=[
        ast.ImportFrom(module='__future__', names=[
            ast.alias(name='annotations')], level=0),
        *functions,
    ], type_ignores=[])
    ast.fix_missing_locations(module)
    # Only integration boundaries are doubled, not the migration algorithm.
    exec(compile(module, str(path), 'exec'), namespace)  # pylint: disable=exec-used
    return namespace


@pytest.fixture(name='migration')
def fixture_migration():
    logger = Mock()
    namespace = load_functions(
        COMPONENT / '__init__.py', ['migrate_service_unique_id'], {
            'DOMAIN': 'xiaomi_home', '_LOGGER': logger})
    device = SimpleNamespace(
        gen_service_entity_id=Mock(return_value='xiaomi_home.new_service'),
        gen_service_entity_id_legacy=Mock(return_value=[
            'xiaomi_home.old_service']),
    )
    return SimpleNamespace(
        migrate=namespace['migrate_service_unique_id'],
        device=device, spec=SimpleNamespace(iid=2, description='fan'),
        logger=logger)


def test_service_migration_preserves_entity_id_and_user_metadata(migration):
    entry = SimpleNamespace(
        entity_id='fan.owner_chosen_name', name='Desk fan',
        area_id='office', aliases={'My fan'})
    entries = {'xiaomi_home.old_service': entry}
    registry = Mock()

    assert migration.migrate(
        registry, entries, migration.device, migration.spec)

    registry.async_update_entity.assert_called_once_with(
        'fan.owner_chosen_name', new_unique_id='xiaomi_home.new_service')
    assert entries == {'xiaomi_home.new_service': entry}
    assert entry.entity_id == 'fan.owner_chosen_name'
    assert entry.name == 'Desk fan'
    assert entry.area_id == 'office'
    assert entry.aliases == {'My fan'}
    registry.async_remove.assert_not_called()


def test_service_migration_conflict_is_visible_and_preserves_registry(
        migration):
    entry = SimpleNamespace(entity_id='fan.owner_chosen_name')
    entries = {'xiaomi_home.old_service': entry}
    registry = Mock()
    registry.async_update_entity.side_effect = ValueError('already registered')

    assert not migration.migrate(
        registry, entries, migration.device, migration.spec)

    assert entries == {'xiaomi_home.old_service': entry}
    assert entry.entity_id == 'fan.owner_chosen_name'
    registry.async_remove.assert_not_called()
    migration.logger.warning.assert_called_once()


@pytest.mark.parametrize('entries', [
    {},
    {'xiaomi_home.new_service': SimpleNamespace(entity_id='fan.current')},
    {
        'xiaomi_home.new_service': SimpleNamespace(entity_id='fan.current'),
        'xiaomi_home.old_service': SimpleNamespace(entity_id='fan.legacy'),
    },
])
def test_service_migration_does_not_rewrite_new_or_current_entries(
        migration, entries):
    registry = Mock()
    previous = dict(entries)

    assert migration.migrate(
        registry, entries, migration.device, migration.spec)

    assert entries == previous
    registry.async_update_entity.assert_not_called()
    registry.async_remove.assert_not_called()


@pytest.mark.parametrize('entity_id', [
    'fan.xiaomi_123_fan_s_2_Air Circulator',
    'light.xiaomi_123_light_s_2_阅读灯',
    'climate.xiaomi_123_aircondition_s_2_Cooling---',
])
def test_upstream_entity_id_normalizes_spec_descriptions(entity_id):
    # The shared slugifier remains the production implementation as well.
    from miot.common import slugify_name  # pylint: disable=import-outside-toplevel

    namespace = load_functions(
        COMPONENT / 'miot/miot_device.py', ['to_valid_entity_id'],
        {'slugify_name': slugify_name})

    normalized = namespace['to_valid_entity_id'](entity_id)

    assert re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', normalized)
    assert normalized.split('.')[0] == entity_id.split('.')[0]
    assert not normalized.endswith('_')


def translation_strings(value):
    """Read every localized form string, including selector labels."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from translation_strings(child)


def test_localized_form_placeholders_are_supplied_for_all_languages():
    tree = ast.parse((COMPONENT / 'config_flow.py').read_text(encoding='utf-8'))
    translations = {
        path.stem: json.loads(path.read_text(encoding='utf-8'))
        for path in (COMPONENT / 'translations').glob('*.json')}
    assert {'en', 'zh-Hans', 'zh-Hant'} <= translations.keys()
    missing = []
    for class_node in tree.body:
        if not isinstance(class_node, ast.ClassDef):
            continue
        section = ('config' if class_node.name == 'XiaomiMihomeConfigFlow'
                   else 'options')
        for call in ast.walk(class_node):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in {
                    'async_show_form', 'async_show_progress', 'async_abort'}
            ):
                continue
            keywords = {item.arg: item.value for item in call.keywords}
            step = keywords.get('step_id', keywords.get('reason'))
            if not isinstance(step, ast.Constant):
                continue
            placeholders = keywords.get('description_placeholders')
            supplied = (
                {key.value for key in placeholders.keys
                 if isinstance(key, ast.Constant)}
                if isinstance(placeholders, ast.Dict) else set())
            kind = 'abort' if call.func.attr == 'async_abort' else 'step'
            for locale, content in translations.items():
                strings = content.get(section, {}).get(kind, {}).get(
                    step.value, {})
                required = {
                    placeholder
                    for text in translation_strings(strings)
                    for placeholder in re.findall(r'{([a-zA-Z_]\w*)}', text)}
                if required - supplied:
                    missing.append((
                        locale, section, step.value,
                        sorted(required - supplied)))
    assert not missing, f'Unresolved localized placeholders: {missing}'
