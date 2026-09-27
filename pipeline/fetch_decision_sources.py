"""Retain configured official decision texts with validated identity and byte hashes.

Only a bounded, explicit source list is fetched. Changed representations remain
content addressed. No old source or observation is overwritten on HTTP/parser
failure. The indexer performs no network access.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
import requests
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.official_decision_sources import PROFILES,extract_official,read_capture

def capture(config,metadata,cache,session,now=None):
 profile=config['profile'];host,media=PROFILES[profile];url=config['url'];parsed=urlsplit(url)
 if parsed.scheme!='https' or parsed.hostname!=host or parsed.username or parsed.password or parsed.port not in {None,443}:raise ValueError('unapproved official source URL')
 if config['decision_id']!=metadata['id']:raise ValueError('decision identity mismatch')
 # Source URLs are selected through official metadata, never generated from a
 # docket or accepted from private case documents.
 with session.get(url,timeout=40,stream=True,allow_redirects=False) as response:
  if response.status_code!=200:raise ValueError(f'official source HTTP {response.status_code}')
  if response.headers.get('Content-Type','').split(';')[0].strip()!=media:raise ValueError('source media type mismatch')
  chunks=[];size=0
  for chunk in response.iter_content(65536):
   size+=len(chunk)
   if size>8*1024*1024:raise ValueError('source exceeds capture bound')
   chunks.append(chunk)
 blob=b''.join(chunks);extract_official(blob,metadata,profile)
 digest=hashlib.sha256(blob).hexdigest();cache.mkdir(parents=True,exist_ok=True)
 previous=read_capture(cache,metadata)
 observed=(now or datetime.now(timezone.utc)).isoformat()
 if previous and previous[1]['sha256']==digest:observed=previous[1]['observed_at']
 record={**config,'sha256':digest,'media_type':media,'observed_at':observed}
 file=cache/(digest+'.source')
 if file.exists() and file.read_bytes()!=blob:raise ValueError('content address collision')
 if not file.exists():
  tmp=file.with_suffix('.tmp');tmp.write_bytes(blob);os.replace(tmp,file)
 index=cache/(metadata['id']+'.json');tmp=index.with_suffix('.tmp');tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n');os.replace(tmp,index)
 return record

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--config',type=Path,default=ROOT/'data/decision_sources.json');parser.add_argument('--cache',type=Path,default=ROOT/'data/cache/official_decisions');args=parser.parse_args()
 metadata={d['id']:d for d in json.loads((ROOT/'data/decisions.json').read_text())['decisions']}
 configs=json.loads(args.config.read_text())
 if len(configs)>100 or len({c['decision_id'] for c in configs})!=len(configs):raise ValueError('unbounded or duplicate source configuration')
 with requests.Session() as session:
  for config in configs:
   result=capture(config,metadata[config['decision_id']],args.cache,session)
   print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
