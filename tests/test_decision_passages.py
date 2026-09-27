import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tools.decision_passages import extract_passages, build_index
from api.decision_passages import create_router

XML='''<?xml version="1.0"?><dokument><doknr>TEST1</doknr><ecli>ECLI:DE:BSG:2026:test</ecli><gertyp>BSG</gertyp><aktenzeichen>B 1 X 1/26 R</aktenzeichen><entsch-datum>20260101</entsch-datum><leitsatz><dl><dt/><dd><p>Editorial overview</p></dd></dl></leitsatz><tenor><dl><dt/><dd><p>Dismissed.</p></dd></dl></tenor><entscheidungsgruende><dl><dt><a name="rd_7">7</a></dt><dd><p>One <em>exact</em> claim.</p><p>Another line.</p></dd></dl><dl><dt/><dd><h2>8. This is a heading, not a paragraph number.</h2></dd></dl><dl><dt><a name="rd_9">9</a></dt><dd><p>Different source statement.</p><table><tr><td>one</td><td>two</td></tr></table></dd></dl></entscheidungsgruende></dokument>'''.encode()
ROW={'id':'rii-test1','court_short':'BSG','az':'B 1 X 1/26 R','date':'2026-01-01','url':'https://www.rechtsprechung-im-internet.de/example','source':'Rechtsprechung im Internet'}
def zipped(xml=XML):
 b=io.BytesIO()
 with zipfile.ZipFile(b,'w',zipfile.ZIP_DEFLATED) as z:z.writestr('jb-TEST1.xml',xml)
 return b.getvalue()

class PassageTests(unittest.TestCase):
 def test_original_numbering_roles_and_multiline_text(self):
  rows=extract_passages(XML,ROW)
  self.assertEqual([r['paragraph_label'] for r in rows],[None,None,'7',None,'9'])
  self.assertEqual(rows[0]['source_role'],'published_headnote')
  self.assertEqual(rows[2]['passage_id'],'entscheidungsgruende:rd_7')
  self.assertEqual(rows[2]['text'],'One exact claim.\nAnother line.')
  self.assertEqual(rows[2]['text_sha256'],hashlib.sha256(rows[2]['text'].encode()).hexdigest())
  self.assertIn('one\ttwo',rows[4]['text'])
 def test_mismatched_identity_duplicate_anchor_and_unrepresented_text_fail(self):
  for xml in [XML.replace(b'TEST1',b'TEST2'),XML.replace(b'rd_9',b'rd_7'),XML.replace(b'<tenor>',b'<tenor><p>orphaned text</p>')]:
   with self.assertRaises(ValueError):extract_passages(xml,ROW)
 def test_entities_and_wrong_court_or_date_fail(self):
  for xml in [b'<!DOCTYPE dokument [<!ENTITY x "bad">]>'+XML, XML.replace(b'<gertyp>BSG',b'<gertyp>BFH'), XML.replace(b'20260101',b'20260102')]:
   with self.assertRaises(ValueError):extract_passages(xml,ROW)

class ApiTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.cache=self.root/'cache';self.cache.mkdir()
  (self.cache/'jb-TEST1.zip').write_bytes(zipped())
  build_index([ROW,{'id':'manual','source':'curated'}],self.cache,self.root,observed_at='2026-09-27T10:00:00+00:00')
  app=FastAPI();app.include_router(create_router(lambda:self.root));self.client=TestClient(app)
 def test_search_exact_read_and_raw_source_agree(self):
  r=self.client.get('/decision-passages',params={'q':'exact claim'});self.assertEqual(r.status_code,200);data=r.json()
  self.assertEqual(data['total'],1);self.assertEqual(data['coverage']['indexed_decisions'],1);self.assertEqual(data['coverage']['metadata_decisions'],2)
  self.assertEqual(data['passages'][0]['paragraph_label'],'7')
  p=self.client.get('/decisions/rii-test1/passages/entscheidungsgruende:rd_7').json()
  self.assertEqual(p['text_sha256'],hashlib.sha256(p['text'].encode()).hexdigest());self.assertEqual(p['source']['xml_sha256'],hashlib.sha256(XML).hexdigest())
  source=self.client.get('/decisions/rii-test1/source');self.assertEqual(source.content,zipped());self.assertEqual(source.headers['x-lexgraph-source-sha256'],hashlib.sha256(source.content).hexdigest())
 def test_exact_scope_pagination_and_source_gaps(self):
  d=self.client.get('/decision-passages?decision_id=rii-test1&section=entscheidungsgruende&limit=1&offset=1').json()
  self.assertEqual(d['total'],3);self.assertIsNone(d['passages'][0]['paragraph_label']);self.assertEqual(d['passages'][0]['section'],'entscheidungsgruende')
  self.assertEqual(self.client.get('/decision-passages?decision_id=manual').status_code,409)
  self.assertEqual(self.client.get('/decision-passages?decision_id=unknown').status_code,404)
  self.assertEqual(self.client.get('/decision-passages?q=missing').json()['total'],0)
 def test_strict_queries_and_fts_injection(self):
  for q in ['','q=x&known_at=2020-01-01','q=x&q=y','q=x&section=bad','q=x&limit=0','q=x&offset=-1']:
   self.assertEqual(self.client.get('/decision-passages?'+q).status_code,422,q)
  self.assertEqual(self.client.get('/decision-passages?q=%22%20OR%20*').status_code,200)
  self.assertEqual(self.client.get('/decisions/rii-test1/passages/entscheidungsgruende:rd_7?at=2020').status_code,422)
 def test_long_passage_excerpt_points_to_matching_source_text(self):
  (self.cache/'jb-TEST1.zip').write_bytes(zipped(XML.replace(b'One <em>',('padding '*450).encode()+b'One <em>')))
  build_index([ROW],self.cache,self.root,observed_at='2026-09-27T11:00:00+00:00')
  hit=self.client.get('/decision-passages?q=exact').json()['passages'][0]
  self.assertIn('exact',hit['excerpt']);self.assertGreater(hit['excerpt_start'],0)
  full=self.client.get('/decisions/rii-test1/passages/entscheidungsgruende:rd_7').json()['text']
  self.assertEqual(hit['excerpt'],full[hit['excerpt_start']:hit['excerpt_end']])
 def test_missing_or_changed_database_is_unavailable_not_zero(self):
  db=self.root/'decision_passages.sqlite';db.write_bytes(b'bad')
  self.assertEqual(self.client.get('/decision-passages?q=test').status_code,503)
  db.unlink();self.assertEqual(self.client.get('/decision-passages?q=test').status_code,503)
 def test_failed_build_keeps_previous_generation(self):
  db=self.root/'decision_passages.sqlite';before=db.read_bytes();(self.cache/'jb-TEST1.zip').write_bytes(zipped(XML.replace(b'TEST1',b'TEST2')))
  with self.assertRaises(ValueError):build_index([ROW],self.cache,self.root,observed_at='2026-09-27T11:00:00+00:00')
  self.assertEqual(db.read_bytes(),before)
if __name__=='__main__':unittest.main()
