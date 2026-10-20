import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_manager import fixture, FakeCollector
from booking_manager import Manager
import booking_file_lock
import naver_room_booking_upgraded as core
from cloud.snapshot import digest, pack, unpack, FILES
from cloud.runner import Bridge, envelope, execute, run
from cloud.build import APP_FILES, web_html

JOB = '9d5db4ef-5c77-4e4c-a461-165c239545ec'


class FakeBridge:
    def __init__(self, stop=False):
        self.stop = stop
        self.calls = []

    def call(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {'stopRequested': self.stop}


class CloudTests(unittest.TestCase):
    def test_browser_runner_is_pinned_to_supported_os(self):
        workflow = (Path(__file__).resolve().parents[1] / "cloud/workflows/collect.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("room_reconcile.py", APP_FILES)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        self.manager = Manager(self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def test_hmac_matches_wire_contract(self):
        value = envelope('x' * 40, {'action': 'claim', 'job_id': JOB, '한글': '객실'}, 123456, JOB)
        material = f"123456\n{JOB}\n{value['payload']}"
        self.assertEqual(value['signature'], hmac.new(b'x' * 40, material.encode(), hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(base64.b64decode(value['payload']))['한글'], '객실')

    def test_bridge_rejects_other_hosts_and_job_injection(self):
        for url in ['http://script.google.com/macros/s/id/exec', 'https://attacker.test/exec', 'https://script.google.com/macros/s/id/exec?secret=x']:
            with self.assertRaises(ValueError):
                Bridge(url, 'x' * 40, JOB, '123:1')
        with self.assertRaises(ValueError):
            Bridge('https://script.google.com/macros/s/id/exec', 'x' * 40, '../job', '123:1')

    def test_round_trip_preserves_data_rules_history(self):
        self.manager.store.meta['room_rules'] = {'123456789': {'notice': {'mode': 'exclude'}}}
        before = self.manager.store.db.read_bytes()
        bundle = pack(self.manager)
        with tempfile.TemporaryDirectory() as temp:
            state = unpack(bundle, temp, digest(bundle))
            restored = Manager(temp)
            self.assertEqual(restored.store.db.read_bytes(), before)
            self.assertTrue(state['cloud'])
            self.assertEqual(restored.rules('123456789')['notice']['mode'], 'exclude')
            self.assertEqual(len(json.loads((Path(temp) / 'history.json').read_text()) ), 3)

    def test_bad_hash_does_not_write(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                unpack(pack(self.manager), temp, '0' * 64)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_zip_traversal_and_duplicates_rejected(self):
        for names in [['../../escape'], ['db.xlsx', 'db.xlsx']]:
            output = io.BytesIO()
            with ZipFile(output, 'w') as z:
                for name in names:
                    z.writestr(name, 'bad')
            with self.assertRaises(ValueError):
                unpack(output.getvalue(), self.folder)

    def test_modified_db_does_not_match_state(self):
        original = pack(self.manager)
        with ZipFile(io.BytesIO(original)) as z:
            values = {n: z.read(n) for n in z.namelist()}
        values['db.xlsx'] += b'changed'
        output = io.BytesIO()
        with ZipFile(output, 'w') as z:
            for name, data in values.items():
                z.writestr(name, data)
        with self.assertRaisesRegex(ValueError, 'revision'):
            unpack(output.getvalue(), self.folder)

    def test_selected_collection_only_once(self):
        key = self.manager.state()['entries'][0]['key']
        payload = {'revision': self.manager.state()['revision'], 'keys': [key], 'date': '2026-09-11'}
        with patch.object(core, 'BrowserCollector', FakeCollector):
            _, progress = execute(FakeBridge(), self.manager, {'kind': 'collect', 'payload': payload})
        self.assertEqual(progress['completed'], 1)
        self.assertEqual(progress['total'], 1)
        self.assertEqual(len(self.manager.data['results']), 1)

    def test_apply_and_collect_uses_one_job_and_only_saved_keys(self):
        state = self.manager.state()
        edit = {**state['entries'][0], 'price': '65000'}
        with patch.object(core, 'BrowserCollector', FakeCollector):
            response, progress = execute(FakeBridge(), self.manager, {'kind': 'apply', 'payload': {
                'revision': state['revision'], 'edits': [edit], 'collect_after': True, 'date': '2026-09-27'}})
        self.assertEqual(response['keys'], [edit['key']])
        self.assertEqual(progress['completed'], 1)
        self.assertEqual(progress['total'], 1)
        self.assertEqual(self.manager.store.entries()[0][0].price, '65000')
        self.assertEqual(list(self.manager.data['results']), [edit['key']])

    def test_invalid_combined_date_cannot_modify_db(self):
        state = self.manager.state()
        before = self.manager.store.db.read_bytes()
        with self.assertRaises(ValueError):
            execute(FakeBridge(), self.manager, {'kind': 'apply', 'payload': {
                'revision': state['revision'], 'edits': [{**state['entries'][0], 'price': '1'}],
                'collect_after': True, 'date': 'not-a-date'}})
        self.assertEqual(before, self.manager.store.db.read_bytes())

    def test_mixed_proposal_batch_is_validated_before_any_write(self):
        state = self.manager.state()
        before = self.manager.store.db.read_bytes()
        edits = [{**state['entries'][0], 'price': '100'},
                 {**state['entries'][1], 'from_proposal': True}]
        with self.assertRaises(ValueError):
            self.manager.apply({'revision': state['revision'], 'edits': edits})
        self.assertEqual(before, self.manager.store.db.read_bytes())

    def test_editor_state_omits_raw_card_text_but_preserves_snapshot(self):
        state = self.manager.state()
        company = state['entries'][0]['company_id']
        self.manager.data['catalogs'][company] = {'snapshot': {'roomCards': [
            {'title': 'A-1', 'itemKey': 'a1', 'text': 'A-1 ' + 'details ' * 1000}]}}
        item = self.manager.state()['entries'][0]['item_groups']['room'][0]
        self.assertNotIn('text', item)
        self.assertEqual(item['itemKey'], 'a1')
        self.assertIn('text', self.manager.data['catalogs'][company]['snapshot']['roomCards'][0])

    def test_stop_before_mutation_keeps_db(self):
        before = self.manager.store.db.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            execute(FakeBridge(stop=True), self.manager, {'kind': 'apply', 'payload': {}})
        self.assertEqual(before, self.manager.store.db.read_bytes())

    def test_archive_uses_existing_validation(self):
        key = self.manager.state()['entries'][0]['key']
        response, outcome = execute(FakeBridge(), self.manager, {'kind': 'archive', 'payload': {
            'keys': [key], 'revision': self.manager.state()['revision'], 'archived': True}})
        self.assertEqual(response, {'count': 1})
        self.assertEqual(outcome['state'], 'completed')
        self.assertTrue(self.manager.state()['entries'][0]['archived'])

    def test_terminal_claim_never_repeats_collection(self):
        bridge = FakeBridge()
        bridge.call = lambda *args, **kwargs: {'terminal': True}
        with patch('booking_manager.Manager') as factory:
            run(bridge)
            factory.assert_not_called()

    def test_lock_contention(self):
        path = self.folder / 'lock'
        path.write_bytes(b'0')
        with path.open('r+b') as first, path.open('r+b') as second:
            booking_file_lock.acquire(first)
            try:
                with self.assertRaises(OSError):
                    booking_file_lock.acquire(second)
            finally:
                booking_file_lock.release(first)
            booking_file_lock.acquire(second)
            booking_file_lock.release(second)

    def test_bundle_html_has_no_secret_or_local_resource_dependency(self):
        html = web_html()
        self.assertNotIn('BRIDGE_SECRET', html)
        self.assertNotIn('GH_TOKEN', html)
        self.assertNotIn('src="/app.js"', html)
        self.assertIn('google.script.run', html)
        self.assertLess(html.index('window.bookingCloudApi ='), html.index('async function api('))

    def test_naver_urls_survive_htmlservice_sanitization(self):
        html = web_html()
        self.assertNotIn('href="https://m.place.naver.com/accommodation/${', html)
        self.assertIn('href="${esc(roomUrl)}"', html)
        self.assertIn(r'const roomUrl = "https:\x2f\x2fm.place.naver.com/accommodation/"', html)
        self.assertIn(r'"https:\x2f\x2fmap.naver.com/p/search/"', html)
        app_js = (Path(__file__).resolve().parents[1] / 'app/web/app.js').read_text(encoding='utf-8')
        self.assertNotIn('https://', app_js)


if __name__ == '__main__':
    unittest.main()
