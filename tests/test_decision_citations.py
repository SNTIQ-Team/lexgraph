import unittest
from tools.decision_citations import citations_for

TARGET={'id':'eu','az':'C-621/24','court_short':'EuGH','court':'Gerichtshof der Europäischen Union'}
ROW={'decision_id':'sg','passage_id':'gruende:block-0001','source_role':'court_reasons_section','text':'Auf die Vorlage des BSG hat der EuGH mit Urteil vom 04.06.2026 (Az.: C‑621/24) erkannt.', 'text_sha256':'a'*64}
class CitationTests(unittest.TestCase):
 def test_exact_qualified_mention_has_offsets_and_source_role(self):
  citations=citations_for(ROW,[TARGET]);self.assertEqual(len(citations),1);c=citations[0]
  self.assertEqual(c['target_id'],'eu');self.assertEqual(c['relation'],'decision_citation')
  self.assertEqual(ROW['text'][c['start']:c['end']],c['quote']);self.assertEqual(c['source_role'],'court_reasons_section')
  self.assertNotIn('follows',str(c))
 def test_wrong_court_substring_ambiguous_identity_and_self_citation_do_not_link(self):
  for row,targets in [({**ROW,'text':ROW['text'].replace('EuGH','BSG')},[TARGET]),({**ROW,'text':ROW['text'].replace('621/24','621/240')},[TARGET]),(ROW,[TARGET,{**TARGET,'id':'duplicate'}]),({**ROW,'decision_id':'eu'},[TARGET])]:
   self.assertEqual(citations_for(row,targets),[])
 def test_unicode_offsets_and_multiple_distinct_mentions(self):
  row={**ROW,'text':'😀 Суд: EuGH, C-621/24; далее: EuGH, C-621/24.'}
  hits=citations_for(row,[TARGET]);self.assertEqual(len(hits),2)
  for h in hits:self.assertEqual(row['text'][h['start']:h['end']],'C-621/24')

class CitationApiTests(unittest.TestCase):
 def test_incoming_and_outgoing_are_source_bound_paginated_and_strict(self):
  import tempfile,json,hashlib
  from pathlib import Path
  from fastapi import FastAPI
  from fastapi.testclient import TestClient
  from tools.decision_passages import build_index
  from api.decision_passages import create_router
  from tests.test_official_decision_sources import BSG,ROW
  blob=BSG.replace(b'27 is only text, not a court paragraph.',b'EuGH, C-621/24 is cited.')
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);cache=root/'official';cache.mkdir();digest=hashlib.sha256(blob).hexdigest();(cache/(digest+'.source')).write_bytes(blob)
   (cache/(ROW['id']+'.json')).write_text(json.dumps({'decision_id':ROW['id'],'profile':'bsg-html/1','url':'https://www.bsg.bund.de/SharedDocs/Entscheidungen/DE/test.html','sha256':digest,'observed_at':'2026-09-27T08:00:00+00:00','media_type':'text/html'}))
   m=build_index([ROW,TARGET],root/'rii',root/'out',observed_at='2026-09-27T09:00:00+00:00',official_cache=cache);self.assertEqual(m['citation_mentions'],1)
   app=FastAPI();app.include_router(create_router(lambda:root/'out'));client=TestClient(app)
   incoming=client.get('/decisions/eu/relations?direction=incoming').json();self.assertEqual(incoming['total'],1)
   proof=incoming['relations'][0];self.assertEqual(proof['source']['content_sha256'],digest)
   exact=client.get('/decisions/'+ROW['id']+'/passages/'+proof['passage_id']).json()
   self.assertEqual(exact['text'][proof['start']:proof['end']],proof['quote']);self.assertEqual(exact['text_sha256'],proof['text_sha256'])
   self.assertEqual(client.get('/decisions/'+ROW['id']+'/relations').json()['total'],1)
   self.assertEqual(client.get('/decisions/eu/relations?direction=incoming&offset=1').json()['relations'],[])
   self.assertEqual(client.get('/decisions/eu/relations').status_code,409)
   for query in ['direction=wrong','known_at=2020-01-01','limit=0','direction=incoming&direction=outgoing']:
    self.assertEqual(client.get('/decisions/eu/relations?'+query).status_code,422)
