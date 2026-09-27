"""Publish only changed watched evidence; unchanged checks stay cheap."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from tools.update_procedure_watch import _fingerprint

ROOT=Path(__file__).resolve().parents[1]
CHECKPOINT=ROOT/'data/cache/watch-publish.json'

def digest(root):
    state=json.loads((root/'data/procedure_watch_state.json').read_text())
    watch=json.loads((root/'data/procedure_watchlist.json').read_text())
    history=(root/'data/procedure_watch_history.jsonl').read_bytes()
    body={'state':{k:_fingerprint(v) for k,v in sorted(state['procedures'].items())},'watch':watch,
          'history_sha256':hashlib.sha256(history).hexdigest()}
    return hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def status(root, checkpoint):
    current=digest(root)
    old=json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    return current,old,current!=old.get('published_digest')

def record(root,checkpoint,published=False):
    current,old,needed=status(root,checkpoint)
    if published:old['published_digest']=current
    old.update(checked_at=datetime.now(timezone.utc).isoformat(),observed_digest=current,publish_needed=needed and not published)
    checkpoint.parent.mkdir(parents=True,exist_ok=True)
    temporary=checkpoint.with_name(checkpoint.name+f'.{os.getpid()}.tmp')
    try:
        with temporary.open('w') as f:
            json.dump(old,f,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(temporary,checkpoint)
    finally:
        temporary.unlink(missing_ok=True)
    return needed

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('op',choices=['needed','published'])
    args=parser.parse_args();needed=record(ROOT,CHECKPOINT,args.op=='published')
    return 3 if args.op=='needed' and not needed else 0

if __name__=='__main__':raise SystemExit(main())
