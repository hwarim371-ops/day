import tempfile
import unittest
from pathlib import Path
from test_manager import fixture
from booking_manager import Manager


class ListOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        fixture(self.folder)
        self.manager = Manager(self.folder)
        self.state = self.manager.state()
        self.keys = [e['key'] for e in self.state['entries']]

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, keys):
        return dict(keys=keys, revision=self.state['revision'], order_revision='')

    def test_order_persists_without_changing_bookings(self):
        db = self.manager.store.db.read_bytes()
        result = self.manager.store.result.read_bytes()
        changed = self.manager.store.meta['changed'][:]
        saved = self.manager.reorder(self.payload(self.keys[::-1]))
        reopened = Manager(self.folder).state()
        self.assertEqual(reopened['display_order'], self.keys[::-1])
        self.assertEqual(reopened['order_revision'], saved['order_revision'])
        self.assertEqual(self.manager.store.db.read_bytes(), db)
        self.assertEqual(self.manager.store.result.read_bytes(), result)
        self.assertEqual(self.manager.store.meta['changed'], changed)

    def test_rejects_duplicate_unknown_missing_and_stale_inputs(self):
        for keys in [self.keys[:-1], [self.keys[0]] * 3, ['unknown'] * 3, None, [{}] * 3]:
            with self.assertRaises(ValueError):
                self.manager.reorder(self.payload(keys))
        saved = self.manager.reorder(self.payload(self.keys[::-1]))
        with self.assertRaises(ValueError):
            self.manager.reorder(self.payload(self.keys))
        self.assertEqual(self.manager.state()['order_revision'], saved['order_revision'])

    def test_archiving_and_restoring_does_not_discard_saved_order(self):
        saved = self.manager.reorder(self.payload(self.keys[::-1]))
        self.manager.archive(dict(keys=[self.keys[1]], revision=self.state['revision']))
        active = [self.keys[2], self.keys[0]]
        self.manager.reorder(dict(self.payload(active), order_revision=saved['order_revision']))
        self.manager.archive(dict(keys=[self.keys[1]], revision=self.state['revision'], archived=False))
        self.assertEqual(set(self.manager.state()['display_order']), set(self.keys))


if __name__ == '__main__':
    unittest.main()
