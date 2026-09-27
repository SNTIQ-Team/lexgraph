"""Compact source observations for local consumers, without editorial outcomes."""
import hashlib
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET

PROFILE='public-decision-observations/1'
NORM=re.compile(r'§\s*(\d+[a-z]?)\s*(?:Abs\.?\s*\d+\s*)?AsylbLG\b',re.I)

def source_type(blob,source,metadata):
 if hashlib.sha256(blob).hexdigest()!=source['content_sha256']:raise ValueError('changed source bytes')
 profile=source['profile']
 if profile=='rii-passages/1':
  try:
   with zipfile.ZipFile(io.BytesIO(blob)) as archive:
    expected='jb-'+metadata['id'][4:]+'.xml'
    members=[m for m in archive.infolist() if m.filename.lower().rsplit('/',1)[-1]==expected]
    if len(members)!=1 or members[0].file_size>16*1024*1024:raise ValueError('invalid RII representation')
    xml=archive.read(members[0])
  except (zipfile.BadZipFile,RuntimeError) as error:raise ValueError('invalid RII archive') from error
  if source.get('xml_sha256') and hashlib.sha256(xml).hexdigest()!=source['xml_sha256']:raise ValueError('changed XML bytes')
  if re.search(br'<!ENTITY\b',xml,re.I):raise ValueError('unsafe XML')
  try:root=ET.fromstring(xml)
  except ET.ParseError as error:raise ValueError('invalid source XML') from error
  if root.tag!='dokument':raise ValueError('unexpected source root')
  for key,tag in [('court_short','gertyp'),('az','aktenzeichen'),('date','entsch-datum')]:
   expected=str(metadata.get(key) or '');actual=' '.join((root.findtext(tag) or '').split())
   if key=='date':expected=expected.replace('-','')
   if not expected or actual.casefold()!=expected.casefold():raise ValueError('source identity mismatch')
  if 'rii-'+(root.findtext('doknr') or '').strip().lower()!=metadata['id']:raise ValueError('source identity mismatch')
  return (root.findtext('doktyp') or '').strip() or None
 # These profiles' identity, court/date/kind and structural checks are mandatory
 # at index build. BSG's heading says Beschluss, not the curated subtype.
 if profile=='bsg-html/1':return 'Beschluss'
 if profile=='sozialgerichtsbarkeit-json/1':
  record=json.loads(blob)
  if record.get('kategorie')!='B':raise ValueError('unsupported official decision type')
  return 'Beschluss'
 if profile=='cellar-xhtml/1':return 'Urteil'
 raise ValueError('unsupported source profile')

def observation(conn,row):
 metadata=json.loads(row['metadata']);source=json.loads(row['source_data'])
 kind=source_type(row['source_zip'],source,metadata)
 mentions={}
 for passage in conn.execute('SELECT data FROM passages WHERE decision_id=? ORDER BY ordinal,passage_id',(row['id'],)):
  block=json.loads(passage['data']);text=block['text']
  if hashlib.sha256(text.encode()).hexdigest()!=block['text_sha256']:raise ValueError('changed source passage')
  for match in NORM.finditer(text):
   norm='asylblg:'+match[1].lower()
   if norm not in mentions:mentions[norm]={'norm':norm,'passage_id':block['passage_id'],'text_sha256':block['text_sha256'],
       'start':match.start(),'end':match.end(),'quote':match[0],'offset_unit':'unicode_codepoints','source_role':block['source_role']}
 return {'id':row['id'],'reference':metadata['az'],'court':metadata.get('court') or metadata['court_short'],
   'court_short':metadata['court_short'],'event_date':metadata['date'],'document_type':kind,
   'ecli':row['ecli'],'published_at':None,'source':source,'norm_mentions':[mentions[k] for k in sorted(mentions)]}
