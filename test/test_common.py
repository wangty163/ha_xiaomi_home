# -*- coding: utf-8 -*-
"""Unit test for miot_common.py."""
import pytest

# pylint: disable=import-outside-toplevel, unused-argument


@pytest.mark.github
def test_miot_matcher():
    from miot.common import MIoTMatcher

    matcher: MIoTMatcher = MIoTMatcher()
    # Add
    for l1 in range(1, 11):
        matcher[f'test/{l1}/#'] = f'test/{l1}/#'
        for l2 in range(1, 11):
            matcher[f'test/{l1}/{l2}'] = f'test/{l1}/{l2}'
            if not matcher.get(topic=f'test/+/{l2}'):
                matcher[f'test/+/{l2}'] = f'test/+/{l2}'
    # Match
    match_result: list[str] = list(matcher.iter_all_nodes())
    assert len(match_result) == 120
    match_result: list[str] = list(matcher.iter_match(topic='test/1/1'))
    assert len(match_result) == 3
    assert set(match_result) == set(['test/1/1', 'test/+/1', 'test/1/#'])
    # Delete
    if matcher.get(topic='test/1/1'):
        del matcher['test/1/1']
    assert len(list(matcher.iter_all_nodes())) == 119
    match_result: list[str] = list(matcher.iter_match(topic='test/1/1'))
    assert len(match_result) == 2
    assert set(match_result) == set(['test/+/1', 'test/1/#'])


@pytest.mark.github
@pytest.mark.parametrize(
    ('rule', 'expected'), [
        ('none', None),
        ('home', 'My Home'),
        ('room', 'Living Room'),
        ('home_room', 'My Home Living Room'),
        ('unsupported', None),
    ])
def test_gen_device_area_name(rule, expected):
    from miot.common import gen_device_area_name

    assert gen_device_area_name(
        device_info={
            'home_name': ' My Home ',
            'room_name': ' Living Room ',
        },
        area_name_rule=rule,
    ) == expected


@pytest.mark.github
def test_gen_device_area_name_skips_empty_values():
    from miot.common import gen_device_area_name

    assert gen_device_area_name(
        device_info={'home_name': 'My Home', 'room_name': ''},
        area_name_rule='room',
    ) is None
    assert gen_device_area_name(
        device_info={'home_name': '', 'room_name': 'Living Room'},
        area_name_rule='home_room',
    ) == 'Living Room'


@pytest.mark.github
def test_gen_device_area_map_uses_xiaomi_identifiers():
    from miot.common import gen_device_area_map

    assert gen_device_area_map(
        devices={
            '12345': {
                'did': '12345',
                'home_name': 'My Home',
                'room_name': 'Living Room',
            },
            'blt.3.test': {
                'did': 'blt.3.test',
                'home_name': 'My Home',
                'room_name': '',
            },
        },
        cloud_server='cn',
        area_name_rule='room',
    ) == {'cn_12345': 'Living Room'}
