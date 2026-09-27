import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_manager import fixture, FakeCollector
from test_cloud import FakeBridge
from booking_manager import Manager
from booking_store import entry_key
import naver_room_booking_upgraded as core
from cloud.runner import execute
from cloud.snapshot import pack, unpack


class ManualPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        self.manager = Manager(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, index=0, **values):
        state = self.manager.state()
        edit = {**state['entries'][index], 'room_authority': 'manual', **values}
        return self.manager.apply({'revision': state['revision'], 'edits': [edit], 'force_manual': True})

    def test_same_list_can_be_confirmed_despite_old_proposal_and_result(self):
        state = self.manager.state()
        key = state['entries'][0]['key']
        self.manager.data['proposals'][key] = {'error': 'old scan', 'review': ['notice']}
        self.manager.data['results'][key] = {'status': 'old error', 'checked_at': '2020', 'fingerprint': ''}
        self.save(from_proposal=True)
        row = self.manager.state()['entries'][0]
        self.assertEqual(row['room_authority'], 'manual')
        self.assertIsNone(row['proposal'])
        self.assertIn('재수집 대기', row['result']['status'])
        self.assertEqual(row['result']['previous_status'], 'old error')

    def test_manual_room_overrides_old_exclusion_and_automatic_review(self):
        self.save(rooms=['A-1'])
        entry = self.manager.store.entries()[0][0]
        self.manager.store.meta['room_rules'] = {entry.company_id: {'a1': {'mode': 'exclude'}}}
        snapshot = {'roomCards': [
            {'itemKey': 'a1', 'title': 'A-1', 'schemaVersion': 3, 'unitEvidence': False, 'statusLabels': ['예약마감']},
            {'itemKey': 'new', 'title': 'NEW', 'schemaVersion': 3, 'unitEvidence': True},
            {'itemKey': 'notice', 'title': '공지사항'}]}
        actual = self.manager.entry_snapshot(entry, snapshot)
        result = core.analyze_entry_snapshot(entry, actual, core.normalize_words(core.DEFAULT_STATUS_WORDS), 'matched', '')
        self.assertEqual((result.status, result.matched_count, result.reserved_count), ('정상', 1, 1))
        proposal = self.manager._proposal(entry, snapshot, self.manager.store.entries()[1])
        self.assertFalse(proposal['changed'])
        self.assertEqual(proposal['proposed'], ['A-1'])
        self.assertEqual(proposal['review'], [])

    def test_proposal_uses_the_same_matching_as_collection(self):
        entry = self.manager.store.entries()[0][2]
        snapshot = {'roomCards': [{'title': '캠핑 B-1'}, {'title': '캠핑 B-2'}]}
        result = core.analyze_entry_snapshot(entry, snapshot, [], 'matched', '')
        self.assertEqual(result.status, '정상')
        proposal = self.manager._proposal(entry, snapshot, self.manager.store.entries()[1])
        self.assertEqual((proposal['added'], proposal['removed'], proposal['changed']), ([], [], False))

    def test_forced_stale_rooms_only_preserves_latest_metadata_and_other_rows(self):
        state = self.manager.state()
        old = state['entries'][0]
        self.save(price='70000', major='updated')
        self.manager.apply({'revision': state['revision'], 'force_manual': True, 'edits': [
            {**old, 'room_authority': 'manual', 'rooms_only': True, 'rooms': ['A-1']}]})
        entries = self.manager.store.entries()[0]
        self.assertEqual((entries[0].price, entries[0].major, entries[0].rooms), ('70000', 'updated', ['A-1']))
        self.assertEqual(entries[1].rooms, ['G-1', 'G-2'])

    def test_force_does_not_allow_automatic_stale_edits_or_unknown_keys(self):
        state = self.manager.state()
        self.save(price='70000')
        before = self.manager.store.db.read_bytes()
        with self.assertRaises(ValueError):
            self.manager.apply({'revision': state['revision'], 'force_manual': True, 'edits': [state['entries'][0]]})
        with self.assertRaises(ValueError):
            self.save(key='gone')
        self.assertEqual(self.manager.store.db.read_bytes(), before)

    def test_manual_priority_persists_through_cloud_snapshot_and_can_be_released(self):
        self.save(rooms=['A-1'])
        with tempfile.TemporaryDirectory() as folder:
            unpack(pack(self.manager), folder)
            restored = Manager(folder)
            self.assertTrue(restored.manual_rooms(restored.store.entries()[0][0]))
        self.save(room_authority='auto')
        self.assertFalse(self.manager.manual_rooms(self.manager.store.entries()[0][0]))

    def test_missing_manual_room_is_unknown_not_zero_booking_success(self):
        self.save(rooms=['A-1', 'NOT-ON-PAGE'])
        state = self.manager.state()
        key = state['entries'][0]['key']
        with patch.object(core, 'BrowserCollector', FakeCollector):
            _, progress = execute(FakeBridge(), self.manager, {'kind': 'collect', 'payload': {
                'keys': [key], 'revision': state['revision'], 'date': '2026-09-27'}})
        row = self.manager.state()['entries'][0]
        self.assertEqual(progress['state'], 'issues')
        self.assertIn('화면에서 확인 못한 객실 1개', row['result']['status'])
        self.assertEqual(row['rooms'], ['A-1', 'NOT-ON-PAGE'])
        self.assertEqual(row['result']['missing_rooms'], ['NOT-ON-PAGE'])
        self.assertFalse(row['changed'])
        self.assertFalse(row['pending_collection'])
        self.assertFalse(row['result']['stale'])
        self.assertNotIn(key, self.manager.store.meta['changed'])
        self.assertEqual(self.manager.store.history(key)[-1]['date'], '2026-08-01')

    def test_collection_failure_after_apply_can_still_publish_manual_db(self):
        state = self.manager.state()
        edit = {**state['entries'][0], 'rooms': ['A-1'], 'room_authority': 'manual'}
        with patch.object(FakeCollector, 'get_snapshot', side_effect=RuntimeError('네이버 접근 제한')), patch.object(core, 'BrowserCollector', FakeCollector):
            response, progress = execute(FakeBridge(), self.manager, {'kind': 'apply', 'payload': {
                'revision': state['revision'], 'edits': [edit], 'collect_after': True, 'date': '2026-09-27'}})
        self.assertTrue(response['collection_failed'])
        self.assertEqual(progress['state'], 'issues')
        with tempfile.TemporaryDirectory() as folder:
            unpack(pack(self.manager), folder)
            restored = Manager(folder)
            self.assertEqual(restored.store.entries()[0][0].rooms, ['A-1'])
            self.assertTrue(restored.manual_rooms(restored.store.entries()[0][0]))
            row = restored.state()['entries'][0]
            self.assertFalse(row['changed'])
            self.assertFalse(row['result']['stale'])
            self.assertTrue(row['result']['status'].startswith('오류'))

    def test_unchanged_save_preserves_current_result(self):
        self.save()
        state = self.manager.state()
        key = state['entries'][0]['key']
        with patch.object(core, 'BrowserCollector', FakeCollector):
            execute(FakeBridge(), self.manager, {'kind': 'collect', 'payload': {
                'keys': [key], 'revision': state['revision'], 'date': '2026-09-27'}})
        before = dict(self.manager.data['results'][key])
        self.save(region='지역 메모 변경')
        row = self.manager.state()['entries'][0]
        self.assertEqual(self.manager.data['results'][key], before)
        self.assertFalse(row['changed'])
        self.assertFalse(row['result']['stale'])
        self.save(price='70000')
        row = self.manager.state()['entries'][0]
        self.assertTrue(row['pending_collection'])
        self.assertTrue(row['result']['superseded'])

    def test_old_changed_flag_with_completed_attempt_is_not_unsaved(self):
        self.test_missing_manual_room_is_unknown_not_zero_booking_success()
        key = self.manager.state()['entries'][0]['key']
        self.manager.store.meta['changed'] = [key]
        row = self.manager.state()['entries'][0]
        self.assertFalse(row['changed'])
        self.assertFalse(row['result']['stale'])
        self.save(rooms=row['rooms'])
        self.assertFalse(self.manager.state()['entries'][0]['pending_collection'])

    def test_priority_follows_rename_and_backups_keep_previous_policy(self):
        saved = self.save()
        old_key = saved['keys'][0]
        renamed = self.save(sheet_title='new title')
        self.assertNotIn(old_key, self.manager.store.meta['manual_rooms'])
        self.assertIn(renamed['keys'][0], self.manager.store.meta['manual_rooms'])
        self.assertTrue((Path(renamed['backup']) / 'catalog.json').exists())

    def test_manual_confirmation_does_not_count_one_card_as_two_rooms(self):
        self.save(rooms=['A-1', '캠핑 A-1'])
        state = self.manager.state()
        snapshot = ('', {'roomCards': [{'title': '캠핑 A-1', 'text': '캠핑 A-1 예약마감'}]})
        with patch.object(FakeCollector, 'get_snapshot', return_value=snapshot), patch.object(core, 'BrowserCollector', FakeCollector):
            _, progress = execute(FakeBridge(), self.manager, {'kind': 'collect', 'payload': {
                'keys': [state['entries'][0]['key']], 'revision': state['revision'], 'date': '2026-09-27'}})
        self.assertEqual(progress['state'], 'issues')
        self.assertIn('중복 집계', self.manager.state()['entries'][0]['result']['status'])

    def test_failed_transaction_restores_manual_policy_with_db(self):
        import os
        before = self.manager.store.db.read_bytes()
        original = os.replace
        def fail_result(source, target):
            if Path(target) == self.manager.store.result:
                raise PermissionError('fixture file lock')
            return original(source, target)
        with patch('booking_store.os.replace', side_effect=fail_result), self.assertRaises(PermissionError):
            self.save()
        self.assertEqual(before, self.manager.store.db.read_bytes())
        self.assertFalse(self.manager.store.meta.get('manual_rooms'))


if __name__ == '__main__':
    unittest.main()
