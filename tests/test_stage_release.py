from pathlib import Path
from types import SimpleNamespace
import os
import pytest
from tools.stage_release import stage
from tools.watch_publish_state import status, record
import json

def test_unchanged_files_share_storage_and_equal_size_changed_bytes_cannot_corrupt_previous(tmp_path):
    source=tmp_path/'source';old=tmp_path/'web-data.release-old';new=tmp_path/'web-data.release-new'
    for p in [source,old,new]:p.mkdir()
    for p in [source,old]:
        (p/'same').write_bytes(b'unchanged'*1000);(p/'changed').write_bytes(b'old')
    (source/'changed').write_bytes(b'new')
    for p in [source,old]:os.utime(p/'changed',(100,100))
    stage(source,new,old,0)
    assert (old/'same').stat().st_ino==(new/'same').stat().st_ino
    assert (old/'changed').read_bytes()==b'old' and (new/'changed').read_bytes()==b'new'
    assert (old/'changed').stat().st_ino!=(new/'changed').stat().st_ino

def test_reserve_and_symlink_refusal_leave_staging_empty(tmp_path,monkeypatch):
    source=tmp_path/'source';target=tmp_path/'target';source.mkdir();target.mkdir();(source/'a').write_text('abc')
    monkeypatch.setattr('tools.stage_release.shutil.disk_usage',lambda p:SimpleNamespace(free=3))
    with pytest.raises(OSError):stage(source,target,None,1)
    assert not list(target.iterdir())
    (source/'link').symlink_to('/etc/passwd')
    with pytest.raises(ValueError):stage(source,target,None,0)

def test_unchanged_checks_skip_rebuild_but_failed_publish_and_new_evidence_retry(tmp_path):
    data=tmp_path/'data';data.mkdir();(data/'procedure_watchlist.json').write_text('{}');(data/'procedure_watch_history.jsonl').write_text('')
    state=data/'procedure_watch_state.json';checkpoint=data/'cache/checkpoint.json'
    state.write_text(json.dumps({'procedures':{'a':{'status':'pending','last_checked':'2020-01-01'}}}))
    assert record(tmp_path,checkpoint)
    assert record(tmp_path,checkpoint), 'observed is not published'
    record(tmp_path,checkpoint,published=True)
    state.write_text(json.dumps({'procedures':{'a':{'status':'pending','last_checked':'2026-01-01'}}}))
    assert not record(tmp_path,checkpoint)
    state.write_text(json.dumps({'procedures':{'a':{'status':'adopted','last_checked':'2026-01-01'}}}))
    assert record(tmp_path,checkpoint)
