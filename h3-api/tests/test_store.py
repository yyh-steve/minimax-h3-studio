import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import tempfile,unittest,concurrent.futures
from h3_api.config import Settings
from h3_api.store import Store

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Settings(data_dir=Path(self.tmp.name),api_key='a'*32,queue_limit=3)
        self.db=Store(self.s)
    def tearDown(self):self.tmp.cleanup()
    def body(self):return {'model':'MiniMax-H3','duration':4,'resolution':'768P','ratio':'1:1','text':'hello','image_source':'data:image/png;base64,eA=='}
    def test_fifo_and_atomic_single_claim(self):
        one,_=self.db.submit(self.body());two,_=self.db.submit(self.body())
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as p:
            result=list(p.map(lambda _:Store(self.s).claim(),range(4)))
        claimed=[r for r in result if r]
        self.assertEqual([r['id'] for r in claimed],[one['id']])
        self.db.fail(one['id'],'failed','Generation failed.')
        self.assertEqual(self.db.claim()['id'],two['id'])
    def test_idempotency_and_conflict(self):
        one,new=self.db.submit(self.body(),'id-key');same,new2=self.db.submit(self.body(),'id-key')
        self.assertTrue(new);self.assertFalse(new2);self.assertEqual(one['id'],same['id'])
        body=self.body();body['duration']=5
        with self.assertRaises(Exception):self.db.submit(body,'id-key')
    def test_recovery_fails_orphans_without_requeue(self):
        one,_=self.db.submit(self.body());self.db.claim();Store(self.s).recover_interrupted()
        self.assertEqual(self.db.get(one['id'])['status'],'failed');self.assertIsNone(self.db.claim())
    def test_queued_cancel_and_terminal_delete(self):
        one,_=self.db.submit(self.body());self.assertEqual(self.db.delete_or_cancel(one['id'])['action'],'cancelled')
        with self.assertRaises(Exception):self.db.delete_or_cancel(one['id'])
        two,_=self.db.submit(self.body());self.db.claim()
        with self.assertRaises(Exception):self.db.delete_or_cancel(two['id'])
        self.db.fail(two['id'],'failed','Generation failed.')
        self.assertEqual(self.db.delete_or_cancel(two['id'])['action'],'deleted')
        self.assertIsNone(self.db.get(two['id']))
    def test_list_filters_and_queue_bound(self):
        ids=[self.db.submit(self.body())[0]['id'] for _ in range(3)]
        with self.assertRaises(Exception):self.db.submit(self.body())
        items,total=self.db.list_jobs(ids=[ids[1]],page_num=1,page_size=20)
        self.assertEqual(total,1);self.assertEqual(items[0]['id'],ids[1])

if __name__=='__main__':unittest.main()
