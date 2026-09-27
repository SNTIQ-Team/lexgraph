import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from tools.official_decision_sources import extract_official, read_capture

BSG = b'''<html><body><div id="readme_9"><h1 class="entscheidungsHeadline"><span class="court">Bundessozialgericht</span><span class="date">Beschluss vom 25.07.2024</span>, B 8 AY 6/23 R</h1><h2>Tenor</h2><p>1. A question is referred.</p><div class="absatz"><h2>Tatbestand</h2><p>Facts with <em>inline</em> text.</p><h2>Entscheidungsgr\xc3\xbcnde</h2><p>27 is only text, not a court paragraph.</p></div></div><div>Not part of decision</div></body></html>'''
ROW = {'id':'bsg-example','court_short':'BSG','az':'B 8 AY 6/23 R','date':'2024-07-25','kind':'Beschluss'}
EU = '''<html xmlns="http://www.w3.org/1999/xhtml"><head><title>62024CJ0621</title></head><body><p>URTEIL DES GERICHTSHOFS</p><p>4. Juni 2026</p><p>In der Rechtssache C‑621/24</p><table><tr><td><p class="coj-count" id="point1">1</p></td><td><p>One <span>claim</span>.</p><p>With continuation.</p></td></tr></table><p>2. An unnumbered heading.</p><table><tr><td><p class="coj-count" id="point2">2</p></td><td><p>Another claim.</p><table><tr><td>a)</td><td>A quoted item.</td></tr></table></td></tr></table><table><tr><td/><td><p>Aus diesen Gründen hat der Gerichtshof für Recht erkannt:</p></td></tr></table><table><tr><td/><td><p>1. Disposition.</p></td></tr></table></body></html>'''.encode()
EU_ROW={'id':'eu-example','court_short':'EuGH','az':'C-621/24','date':'2026-06-04','kind':'Urteil','celex':'62024CJ0621'}

class OfficialTests(unittest.TestCase):
 def test_bsg_does_not_invent_paragraphs_from_order_or_item_labels(self):
  rows,ecli=extract_official(BSG,ROW,'bsg-html/1')
  self.assertEqual([r['section'] for r in rows],['titelzeile','tenor','tatbestand','entscheidungsgruende'])
  self.assertTrue(all(r['paragraph_label'] is None for r in rows))
  self.assertEqual(rows[2]['text'],'Facts with inline text.')
  self.assertEqual(rows[0]['source_locator'],'#readme_9 > h1:nth-of-type(1)')
  self.assertNotIn('Not part', ' '.join(r['text'] for r in rows))
 def test_bsg_wrong_identity_or_unknown_sections_fail(self):
  for data in [BSG.replace(b'6/23',b'7/23'),BSG.replace(b'25.07.2024',b'26.07.2024'),BSG.replace(b'Tatbestand',b'Unknown'),BSG.replace(b'<p>Facts',b'<p id="rn1">Facts')]:
   with self.assertRaises(ValueError):extract_official(data,ROW,'bsg-html/1')
 def test_eu_original_anchor_number_and_quoted_structure_survive(self):
  rows,_=extract_official(EU,EU_ROW,'cellar-xhtml/1')
  numbered=[r for r in rows if r['paragraph_label']]
  self.assertEqual([r['paragraph_label'] for r in numbered],['1','2'])
  self.assertEqual(numbered[0]['passage_id'],'entscheidungsgruende:point1')
  self.assertEqual(numbered[0]['text'],'One claim.\nWith continuation.')
  self.assertIn('a)\tA quoted item.',numbered[1]['text'])
  self.assertTrue(all(r['source_role']=='court_disposition' for r in rows[-2:]))
  self.assertTrue(all(hashlib.sha256(r['text'].encode()).hexdigest()==r['text_sha256'] for r in rows))
 def test_eu_missing_duplicate_mislabelled_anchors_and_wrong_identity_fail(self):
  for data in [EU.replace(b'point2',b'point1'),EU.replace(b'point2',b'point3'),EU.replace(b'>2</p>',b'>4</p>'),EU.replace(b'62024CJ0621',b'62024CJ0622'),EU.replace(b'4. Juni',b'5. Juni'),EU.replace(b'<body>',b'<body>unrepresented')]:
   with self.assertRaises(ValueError):extract_official(data,EU_ROW,'cellar-xhtml/1')
 def test_capture_must_match_original_bytes_identity_and_official_origin(self):
  with tempfile.TemporaryDirectory() as temp:
   cache=Path(temp);digest=hashlib.sha256(BSG).hexdigest();(cache/(digest+'.source')).write_bytes(BSG)
   record={'decision_id':ROW['id'],'profile':'bsg-html/1','url':'https://www.bsg.bund.de/SharedDocs/Entscheidungen/DE/test.html','sha256':digest,'observed_at':'2026-09-27T08:00:00+00:00','media_type':'text/html'}
   file=cache/(ROW['id']+'.json');file.write_text(json.dumps(record))
   self.assertEqual(read_capture(cache,ROW)[0],BSG)
   for patch in [{'decision_id':'other'},{'url':'https://example.org/test'},{'sha256':'0'*64},{'observed_at':'2026-01-01'}]:
    file.write_text(json.dumps({**record,**patch}))
    with self.assertRaises((ValueError,FileNotFoundError)):read_capture(cache,ROW)

class MixedIndexTests(unittest.TestCase):
 def test_official_html_retains_bytes_and_observation_across_rebuilds(self):
  from fastapi import FastAPI
  from fastapi.testclient import TestClient
  from tools.decision_passages import build_index
  from api.decision_passages import create_router
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);cache=root/'official';cache.mkdir();digest=hashlib.sha256(EU).hexdigest()
   (cache/(digest+'.source')).write_bytes(EU)
   (cache/(EU_ROW['id']+'.json')).write_text(json.dumps({'decision_id':EU_ROW['id'],'profile':'cellar-xhtml/1','url':'https://publications.europa.eu/resource/cellar/test/DOC_1','sha256':digest,'observed_at':'2026-09-27T08:00:00+00:00','media_type':'application/xhtml+xml'}))
   for stamp in ['2026-09-27T09:00:00+00:00','2026-09-28T09:00:00+00:00']:
    m=build_index([EU_ROW],root/'rii',root/'output',observed_at=stamp,official_cache=cache)
    self.assertEqual(m['indexed_decisions'],1)
   app=FastAPI();app.include_router(create_router(lambda:root/'output'));client=TestClient(app)
   response=client.get('/decisions/eu-example/passages/entscheidungsgruende:point2');self.assertEqual(response.status_code,200)
   data=response.json();self.assertEqual(data['source']['content_sha256'],digest);self.assertIsNone(data['source']['zip_sha256'])
   self.assertEqual(data['source']['observed_at'],'2026-09-27T08:00:00+00:00')
   raw=client.get('/decisions/eu-example/source');self.assertEqual(raw.content,EU);self.assertIn('application/xhtml+xml',raw.headers['content-type'])
   self.assertIn('filename="eu-example.xhtml"',raw.headers['content-disposition'])
   self.assertEqual(raw.headers['x-lexgraph-source-sha256'],digest)

class PortalTests(unittest.TestCase):
 def test_portal_json_checks_case_court_date_and_does_not_invent_numbering(self):
  row={'id':'sg-example','court':'Sozialgericht München','court_short':'SG München','az':'S 42 AY 55/26 ER','date':'2026-07-06','kind':'Beschluss'}
  source={'id':180475,'aktenzeichen':row['az'],'gericht':{'bezeichnung':row['court'],'instanz':'SG'},'datum_1_instanz':row['date'],'kategorie':'B','text':'&lt;p&gt;I. Granted.&lt;/p&gt;&lt;p&gt;G r ü n d e :&lt;/p&gt;&lt;p&gt;28 is text.&lt;/p&gt;','leitsaetze':'Published headnote'}
  data=json.dumps(source).encode();rows,_=extract_official(data,row,'sozialgerichtsbarkeit-json/1')
  self.assertEqual([r['section'] for r in rows],['leitsatz','tenor','gruende','gruende'])
  self.assertTrue(all(r['paragraph_label'] is None for r in rows))
  self.assertEqual(rows[-1]['source_locator'],'/text :: p:nth-of-type(3)')
  for change in [{'aktenzeichen':'S 42 AY 56/26 ER'},{'datum_1_instanz':'2026-07-07'},{'gericht':{'bezeichnung':'Other','instanz':'SG'}},{'text':'&lt;script&gt;bad&lt;/script&gt;'}]:
   with self.assertRaises(ValueError):extract_official(json.dumps({**source,**change}).encode(),row,'sozialgerichtsbarkeit-json/1')

class CaptureTests(unittest.TestCase):
 def test_failed_or_changed_http_content_cannot_replace_retained_source(self):
  from pipeline.fetch_decision_sources import capture
  from datetime import datetime,timezone
  class Response:
   def __init__(self,data,status=200,media='text/html'):self.data=data;self.status_code=status;self.headers={'Content-Type':media}
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def iter_content(self,size):yield self.data
  class Session:
   def __init__(self,r):self.r=r
   def get(self,*args,**kwargs):return self.r
  config={'decision_id':ROW['id'],'profile':'bsg-html/1','url':'https://www.bsg.bund.de/SharedDocs/Entscheidungen/DE/test.html'}
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);now=datetime(2026,9,27,8,tzinfo=timezone.utc)
   good=capture(config,ROW,root,Session(Response(BSG)),now);file=root/(ROW['id']+'.json');before=file.read_bytes()
   again=capture(config,ROW,root,Session(Response(BSG)),datetime(2026,9,28,8,tzinfo=timezone.utc));self.assertEqual(again['observed_at'],good['observed_at'])
   for response in [Response(b'',202),Response(b'<html>Security Check</html>'),Response(BSG.replace(b'6/23',b'7/23')),Response(BSG,media='application/json')]:
    with self.assertRaises(ValueError):capture(config,ROW,root,Session(response),now)
    self.assertEqual(file.read_bytes(),before)
