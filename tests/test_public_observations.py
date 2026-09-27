import unittest
from tests import test_decision_passages as fixtures
ROW,XML,zipped=fixtures.ROW,fixtures.XML,fixtures.zipped
from tools.decision_passages import build_index

class ObservationTests(unittest.TestCase):
 setUp=fixtures.ApiTests.setUp
 def test_retained_feed_has_exact_clock_type_and_norm_evidence(self):
  xml=XML.replace(b'<gertyp>',b'<doktyp>Urteil</doktyp><gertyp>').replace(b'One <em>exact</em> claim.', '§ 1a Abs. 1 AsylbLG. § 1a AsylbLG.'.encode())
  (self.cache/'jb-TEST1.zip').write_bytes(zipped(xml))
  row={**ROW,'kind':'Curated outcome','summary':{'de':'Won everything'},'effects':[{'jurabk':'AsylbLG','paras':['999']}]}
  build_index([row,{'id':'manual','source':'curated'}],self.cache,self.root,observed_at='2026-09-27T11:00:00+00:00')
  response=self.client.get('/decision-observations?limit=1&offset=0');self.assertEqual(response.status_code,200)
  data=response.json();self.assertEqual(data['status'],'partial');self.assertEqual(data['total'],1)
  self.assertEqual(data['request_scope'],{'limit':1,'offset':0});self.assertEqual(data['profile'],'public-decision-observations/1')
  obs=data['observations'][0];self.assertEqual(obs['id'],ROW['id']);self.assertEqual(obs['document_type'],'Urteil')
  self.assertIsNone(obs['published_at']);self.assertEqual(obs['source']['observed_at'],'2026-09-27T11:00:00+00:00')
  self.assertEqual([n['norm'] for n in obs['norm_mentions']],['asylblg:1a'])
  for proof in obs['norm_mentions']:
   full=self.client.get('/decisions/rii-test1/passages/'+proof['passage_id']).json()
   self.assertEqual(proof['text_sha256'],full['text_sha256']);self.assertEqual(proof['quote'],full['text'][proof['start']:proof['end']])
  self.assertNotIn('summary',obs);self.assertNotIn('effects',obs);self.assertNotIn('outcome',obs)
  self.assertEqual(self.client.get('/decision-observations?limit=1&offset=1').json()['observations'],[])
 def test_observation_scope_and_integrity_are_never_silently_dropped(self):
  for query in ['known_at=2020-01-01','offset=0&offset=1','q=anything','limit=101','limit=0']:
   self.assertEqual(self.client.get('/decision-observations?'+query).status_code,422)
  (self.root/'decision_passages.sqlite').write_bytes(b'bad')
  r=self.client.get('/decision-observations');self.assertEqual(r.status_code,503)
 def test_corrupt_source_representations_fail_with_integrity_status(self):
  import hashlib
  from api.public_observations import source_type
  for blob in [b'not a zip',zipped(b'<dokument>broken')]:
   descriptor={'profile':'rii-passages/1','content_sha256':hashlib.sha256(blob).hexdigest()}
   with self.assertRaises(ValueError):source_type(blob,descriptor,{**ROW,'id':'rii-test1'})
  blob=zipped(XML)
  with self.assertRaises(ValueError):source_type(blob,{'profile':'rii-passages/1','content_sha256':hashlib.sha256(blob).hexdigest(),'xml_sha256':'0'*64},{**ROW,'id':'rii-test1'})
