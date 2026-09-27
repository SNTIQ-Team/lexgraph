"""Build a compact source-bound passage index from retained court publications.
No network and no publication/knowledge dates inferred from filesystem mtimes.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0, str(ROOT / 'pipeline'))
from fetch_rii import parse_xml, read_zip_xml
from tools.official_decision_sources import read_capture, extract_official
from tools.decision_citations import citations_for

SECTIONS = {
 'titelzeile':'published_title', 'leitsatz':'published_headnote',
 'sonstosatz':'published_headnote', 'tenor':'court_disposition',
 'tatbestand':'court_reported_facts', 'entscheidungsgruende':'court_reasoning_section',
 'gruende':'court_reasoning_section', 'abwmeinung':'separate_opinion', 'sonstlt':'published_other',
}
def sha(data):return hashlib.sha256(data).hexdigest()
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def text_content(node):
 def walk(el):
  text=el.text or ''
  for child in el:
   text+=walk(child)+(child.tail or '')
  if el.tag in {'p','div','h1','h2','h3','h4','tr','br','li','dd','dl'}:text+='\n'
  elif el.tag in {'td','th'}:text+='\t'
  return text
 # Keep paragraph/table boundaries while removing XML indentation whitespace.
 return '\n'.join('\t'.join(' '.join(cell.split()) for cell in line.split('\t')).strip() for line in walk(node).splitlines() if line.strip()).strip()

def extract_passages(xml:bytes,metadata:dict):
 root=parse_xml(xml,label=metadata['id'])
 if root.tag!='dokument':raise ValueError('unexpected source root')
 doknr=(root.findtext('doknr') or '').strip()
 if metadata['id']!='rii-'+doknr.lower():raise ValueError('decision identity mismatch')
 for key,tag in [('court_short','gertyp'),('az','aktenzeichen'),('date','entsch-datum')]:
  expected=str(metadata.get(key) or '')
  if key=='date':expected=expected.replace('-','')
  actual=' '.join((root.findtext(tag) or '').split())
  if not expected or expected.casefold()!=actual.casefold():raise ValueError(f'decision {key} mismatch')
 rows=[]
 for section,role in SECTIONS.items():
  nodes=root.findall(section)
  if len(nodes)>1:raise ValueError('duplicate source section')
  if not nodes:continue
  node=nodes[0];blocks=[]
  def collect(el,path):
   if el.tag=='dl':
    if el.find('.//dl') is not None:raise ValueError('nested source paragraph structure')
    blocks.append((el,path));return
   if (el.text or '').strip():raise ValueError('unrepresented source text')
   counts={}
   for child in el:
    counts[child.tag]=counts.get(child.tag,0)+1
    collect(child,f'{path}/{child.tag}[{counts[child.tag]}]')
    if (child.tail or '').strip():raise ValueError('unrepresented source tail')
  collect(node,f'/dokument/{section}[1]')
  seen=set()
  for ordinal,(block,xpath) in enumerate(blocks,1):
   if len(block.findall('dt'))!=1 or len(block.findall('dd'))!=1 or len(block)!=2:raise ValueError('unsupported source block')
   dt,dd=block.find('dt'),block.find('dd');label=text_content(dt)
   anchors=[a.get('name') for a in dt.iter('a') if a.get('name')]
   if len(anchors)>1:raise ValueError('ambiguous source anchor')
   anchor=anchors[0] if anchors else None
   if anchor and not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',anchor):raise ValueError('invalid source anchor')
   if label and not anchor:raise ValueError('label without source anchor')
   pid=f'{section}:{anchor or "block-"+str(ordinal).zfill(4)}'
   if pid in seen:raise ValueError('duplicate source anchor')
   seen.add(pid);text=text_content(dd)
   if not text:continue
   rows.append({'decision_id':metadata['id'],'passage_id':pid,'section':section,'ordinal':ordinal,
      'document_position':len(rows)+1,'source_role':role,'source_anchor':anchor,'source_label':label or None,
      'paragraph_label':label if anchor and re.fullmatch(r'\d+[a-z]?',label) else None,
      'xml_path':xpath+'/dd[1]','text':text,'text_sha256':sha(text.encode())})
 return rows

def build_index(decisions,cache:Path,destination:Path,*,observed_at:str,official_cache:Path|None=None):
 stamp=datetime.fromisoformat(observed_at)
 if stamp.tzinfo is None:raise ValueError('observation time requires a timezone')
 destination=destination.resolve()
 official_cache=official_cache or cache.parent / "official_decisions"
 destination.mkdir(parents=True,exist_ok=True)
 available={p.stem.lower():p for p in cache.glob('*.zip')}
 if len({d['id'] for d in decisions})!=len(decisions):raise ValueError('duplicate decision identity')
 target=destination/'decision_passages.sqlite';previous={}
 if target.exists():
  with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as old:
   columns={r[1] for r in old.execute('PRAGMA table_info(decisions)')}
   field='content_sha256' if 'content_sha256' in columns else 'zip_sha256'
   previous={r[0]:(r[1],r[2]) for r in old.execute(f'SELECT id, {field}, observed_at FROM decisions WHERE {field} IS NOT NULL')}
 fd,tmp=tempfile.mkstemp(prefix='.decision-passages-',suffix='.sqlite',dir=destination);os.close(fd)
 indexed=0;passages=0;missing=[]
 try:
  with closing(sqlite3.connect(tmp)) as db:
   db.executescript('''CREATE TABLE decisions(id TEXT PRIMARY KEY, status TEXT NOT NULL, metadata TEXT NOT NULL,
       ecli TEXT, xml_sha256 TEXT, zip_sha256 TEXT, observed_at TEXT, source_zip BLOB, content_sha256 TEXT, source_data TEXT);
    CREATE TABLE passages(decision_id TEXT NOT NULL, passage_id TEXT NOT NULL, section TEXT NOT NULL,
       ordinal INTEGER NOT NULL, text TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(decision_id,passage_id));
    CREATE VIRTUAL TABLE passage_fts USING fts5(text,content='passages',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
    CREATE INDEX passage_scope ON passages(decision_id,section,ordinal);
    CREATE TABLE citations(source_id TEXT NOT NULL,target_id TEXT NOT NULL,passage_id TEXT NOT NULL,position INTEGER NOT NULL,data TEXT NOT NULL);
    CREATE INDEX cited_decision ON citations(target_id,source_id,passage_id,position);
    CREATE INDEX citing_decision ON citations(source_id,target_id,passage_id,position);''')
   for d in sorted(decisions,key=lambda d:d['id']):
    identity=d['id'];source=available.get('jb-'+identity[4:]) if identity.startswith('rii-') else None
    capture=read_capture(official_cache,d) if not source else None
    status='indexed' if source or capture else 'source_not_retained'
    if status!='indexed':
     missing.append({'decision_id':identity,'reason':status})
     db.execute('INSERT INTO decisions(id,status,metadata) VALUES(?,?,?)',(identity,status,canonical(d)));continue
    xml_hash=zip_hash=None
    if source:
     blob=source.read_bytes();xml=read_zip_xml(blob,source.stem);rows=extract_passages(xml,d)
     xml_hash=sha(xml);zip_hash=sha(blob);observed=observed_at
     ecli=parse_xml(xml,label=identity).findtext('ecli') or None
     descriptor={'profile':'rii-passages/1','url':d.get('url'),'media_type':'application/zip','extension':'zip',
                 'xml_sha256':xml_hash,'zip_sha256':zip_hash}
    else:
     blob,record=capture;rows,ecli=extract_official(blob,d,record['profile']);observed=record['observed_at']
     descriptor={'profile':record['profile'],'url':record['url'],'media_type':record['media_type'],
                 'extension':{'cellar-xhtml/1':'xhtml','sozialgerichtsbarkeit-json/1':'json','bsg-html/1':'html'}[record['profile']],
                 'canonical_url':d.get('url'),'xml_sha256':None,'zip_sha256':None}
    content_hash=sha(blob);prior=previous.get(identity)
    first_seen=prior[1] if prior and prior[0]==content_hash else observed
    if datetime.fromisoformat(first_seen)>stamp:raise ValueError('observation after index build')
    descriptor.update(content_sha256=content_hash,observed_at=first_seen,language='de',lineage_key=ecli or identity)
    db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)',(identity,status,canonical(d),ecli,xml_hash,zip_hash,first_seen,blob,content_hash,canonical(descriptor)))
    for row in rows:
     row['citations']=citations_for(row,decisions)
     for citation in row['citations']:
      db.execute('INSERT INTO citations VALUES(?,?,?,?,?)',(identity,citation['target_id'],row['passage_id'],citation['start'],canonical(citation)))
     cur=db.execute('INSERT INTO passages VALUES(?,?,?,?,?,?)',(identity,row['passage_id'],row['section'],row['document_position'],row['text'],canonical(row)))
     db.execute('INSERT INTO passage_fts(rowid,text) VALUES(?,?)',(cur.lastrowid,re.sub(r'(?<=\w)-\n(?=\w)','',row['text'])))
    indexed+=1;passages+=len(rows)
   citation_count=db.execute('SELECT count(*) FROM citations').fetchone()[0]
   db.commit()
   if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('database integrity check failed')
  data=Path(tmp).read_bytes()
  manifest={'schema_version':2,'profile':'official-passages/2','built_at':observed_at,'database_sha256':sha(data),
    'bytes':len(data),'metadata_decisions':len(decisions),'indexed_decisions':indexed,'passages':passages,
    'citation_mentions':citation_count,'gaps':missing,'knowledge_time':'first_retention_or_legacy_index_observation; publication dates unresolved',
    'text_normalization':'source inline text; newline block boundaries; tab table cells; collapsed formatting whitespace'}
  os.replace(tmp,target)
  m=destination/'.decision-passages-manifest.tmp';m.write_text(canonical(manifest)+'\n',encoding='utf8');os.replace(m,destination/'decision_passages.json')
  return manifest
 finally:Path(tmp).unlink(missing_ok=True)

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--decisions',type=Path,required=True);p.add_argument('--cache',type=Path,required=True);p.add_argument('--destination',type=Path,required=True);p.add_argument('--observed-at',required=True);a=p.parse_args()
 print(json.dumps(build_index(json.loads(a.decisions.read_text()),a.cache,a.destination,observed_at=a.observed_at),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
