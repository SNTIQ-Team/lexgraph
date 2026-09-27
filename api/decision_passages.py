"""Bounded passage discovery and exact original-block/source retrieval."""
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import quote
from fastapi import APIRouter,HTTPException,Query,Request
from fastapi.responses import Response
SECTIONS={'titelzeile','leitsatz','sonstosatz','tenor','tatbestand','entscheidungsgruende','gruende','abwmeinung','sonstlt'}

def failure(code,status,**detail):raise HTTPException(code,{'status':status,**detail})
def validate_query(request,allowed):
 unknown=sorted(set(request.query_params)-allowed)
 repeated=sorted(k for k in request.query_params if len(request.query_params.getlist(k))>1)
 if unknown or repeated:
  failure(422,'unsupported_temporal_request' if set(unknown)&{'at','as_of','known_at','valid_at'} else 'invalid_request',unsupported_arguments=unknown,repeated_arguments=repeated)

def stamp(file):
 s=file.stat();return (s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
@lru_cache(maxsize=8)
def verified(root,db_stamp,manifest_stamp):
 root=Path(root);manifest=json.loads((root/'decision_passages.json').read_text())
 if manifest.get('schema_version')!=1 or manifest.get('profile')!='rii-passages/1':raise ValueError('invalid manifest')
 with (root/'decision_passages.sqlite').open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
 if digest!=manifest.get('database_sha256'):raise ValueError('database hash mismatch')
 return manifest

def database(root):
 try:
  db=root/'decision_passages.sqlite';manifest=root/'decision_passages.json'
  coverage=verified(str(root),stamp(db),stamp(manifest))
  conn=sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True);conn.row_factory=sqlite3.Row
  return conn,coverage
 except FileNotFoundError:failure(503,'source_unavailable',reason='decision_passage_index_missing')
 except (OSError,ValueError,sqlite3.Error):failure(503,'integrity_check_failed',reason='decision_passage_index_unusable')

def coverage_status(root):
 try:
  conn,coverage=database(Path(root).resolve());conn.close()
  return {'status':'ok',**{k:coverage[k] for k in ['profile','metadata_decisions','indexed_decisions','passages','gaps','knowledge_time']}}
 except HTTPException as error:return error.detail

def decision(conn,identity):
 row=conn.execute('SELECT id,status,metadata,ecli,xml_sha256,zip_sha256,observed_at FROM decisions WHERE id=?',(identity,)).fetchone()
 if row is None:failure(404,'not_in_corpus',decision_id=identity)
 if row['status']!='indexed':failure(409,'source_text_unavailable',decision_id=identity,reason=row['status'])
 return row

def enrich(row,source,*,preview=False,tokens=()):
 data=json.loads(row['data']);metadata=json.loads(source['metadata'])
 data['decision']={k:metadata.get(k) for k in ['id','az','court_short','date','kind']};data['decision']['ecli']=source['ecli']
 data['source']={'url':metadata.get('url'),'xml_sha256':source['xml_sha256'],'zip_sha256':source['zip_sha256'],'observed_at':source['observed_at'],'profile':'rii-passages/1','language':'de'}
 if preview:
  text=data.pop('text');matches=[re.search(r'\b'+re.escape(t)+r'\b',text,re.IGNORECASE) for t in tokens]
  positions=[m.start() for m in matches if m];start=max(0,min(positions)-160) if positions else 0
  end=min(len(text),start+800)
  data.update(text_characters=len(text),excerpt=text[start:end],excerpt_start=start,excerpt_end=end,offset_unit='unicode_codepoints',truncated=start>0 or end<len(text))
 return data

def create_router(data_dir):
 router=APIRouter()
 @router.get('/decision-passages')
 def search(request:Request,q:str|None=Query(None,min_length=1,max_length=400),decision_id:str|None=Query(None,min_length=1,max_length=200),section:str|None=None,limit:int=Query(10,ge=1,le=25),offset:int=Query(0,ge=0,le=100000)):
  validate_query(request,{'q','decision_id','section','limit','offset'})
  if q is not None and not q.strip():failure(422,'invalid_request',reason='q must not be blank')
  if not(q and q.strip()) and not decision_id:failure(422,'invalid_request',reason='q or decision_id required')
  if section is not None and section not in SECTIONS:failure(422,'invalid_request',reason='unknown section')
  conn,coverage=database(Path(data_dir()).resolve())
  try:
   if decision_id:decision(conn,decision_id)
   params=[];where=[];join=''
   tokens=re.findall(r'[^\W_]+',q or '',re.UNICODE)
   if q:
    if not tokens:return {'status':'ok','request_scope':{'q':q,'decision_id':decision_id,'section':section,'limit':limit,'offset':offset},'total':0,'passages':[],'coverage':coverage}
    join=' JOIN passage_fts ON passage_fts.rowid=p.rowid';where.append('passage_fts MATCH ?');params.append(' AND '.join('"'+t+'"' for t in tokens))
   if decision_id:where.append('p.decision_id=?');params.append(decision_id)
   if section:where.append('p.section=?');params.append(section)
   suffix=' FROM passages p'+join+(' WHERE '+' AND '.join(where) if where else '')
   total=conn.execute('SELECT count(*)'+suffix,params).fetchone()[0]
   order='bm25(passage_fts),p.decision_id,p.ordinal' if q else 'p.decision_id,p.ordinal'
   rows=conn.execute('SELECT p.*'+suffix+' ORDER BY '+order+' LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
   sources={r['decision_id']:decision(conn,r['decision_id']) for r in rows}
   return {'status':'ok','request_scope':{'q':q,'decision_id':decision_id,'section':section,'limit':limit,'offset':offset},'total':total,'passages':[enrich(r,sources[r['decision_id']],preview=True,tokens=tokens) for r in rows],'coverage':coverage}
  except (sqlite3.Error,ValueError):failure(503,'integrity_check_failed',reason='decision_passage_index_unusable')
  finally:conn.close()
 @router.get('/decisions/{decision_id}/passages/{passage_id}')
 def exact(request:Request,decision_id:str,passage_id:str):
  validate_query(request,set());conn,coverage=database(Path(data_dir()).resolve())
  try:
   source=decision(conn,decision_id);row=conn.execute('SELECT * FROM passages WHERE decision_id=? AND passage_id=?',(decision_id,passage_id)).fetchone()
   if row is None:failure(404,'not_in_corpus',decision_id=decision_id,passage_id=passage_id)
   result=enrich(row,source)
   if hashlib.sha256(result['text'].encode()).hexdigest()!=result['text_sha256']:failure(503,'integrity_check_failed')
   return {'status':'ok',**result}
  except (sqlite3.Error,ValueError):failure(503,'integrity_check_failed')
  finally:conn.close()
 @router.get('/decisions/{decision_id}/source')
 def source(request:Request,decision_id:str):
  validate_query(request,set());conn,_=database(Path(data_dir()).resolve())
  try:
   row=decision(conn,decision_id);blob=conn.execute('SELECT source_zip FROM decisions WHERE id=?',(decision_id,)).fetchone()[0]
   if hashlib.sha256(blob).hexdigest()!=row['zip_sha256']:failure(503,'integrity_check_failed')
   return Response(blob,media_type='application/zip',headers={'X-Lexgraph-Source-Sha256':row['zip_sha256'],'Content-Disposition':f'attachment; filename="{quote(decision_id,safe="")}.zip"'})
  except (sqlite3.Error,ValueError):failure(503,'integrity_check_failed')
  finally:conn.close()
 return router
