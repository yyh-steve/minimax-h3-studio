import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from h3_api.contracts import normalize_request
from h3_api.errors import ApiError
class ContractTests(unittest.TestCase):
    def body(self):return {'model':'MiniMax-H3','duration':4,'resolution':'768P','content':[{'type':'text','text':'hello'},{'type':'image_url','role':'reference_image','image_url':{'url':'https://example.com/a.png'}}]}
    def test_normalize(self):
        p=normalize_request(self.body());self.assertEqual(p['text'],'hello');self.assertEqual(p['ratio'],'adaptive')
    def test_strict_duration(self):
        for value in [True,4.0,'4',3,16,None]:
            b=self.body();b['duration']=value
            with self.subTest(value=value),self.assertRaises(ApiError):normalize_request(b)
    def test_no_silent_unsupported(self):
        for name,value in [('callback_url','https://example.com'),('extra',{}),('seed',1),('aigc_watermark',True)]:
            b=self.body();b[name]=value
            with self.subTest(name=name),self.assertRaises(ApiError):normalize_request(b)
    def test_single_reference_explicit_role_and_text(self):
        for mutation in ('missing_role','duplicate','empty'):
            b=self.body()
            if mutation=='missing_role':del b['content'][1]['role']
            elif mutation=='duplicate':b['content'].append(b['content'][1])
            else:b['content'][0]['text']=' '
            with self.subTest(mutation=mutation),self.assertRaises(ApiError):normalize_request(b)
if __name__=='__main__':unittest.main()
