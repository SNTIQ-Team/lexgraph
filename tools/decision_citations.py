"""Literal court-qualified citations, not legal-treatment or causality labels."""
import re

def key(az):return re.sub(r'\s+','',az).replace('‑','-').replace('–','-').casefold()
def citations_for(passage,decisions):
 targets={}
 for d in decisions:
  if d.get('az') and d.get('court_short'):targets.setdefault(key(d['az']),[]).append(d)
 citations=[];text=passage['text']
 for candidates in targets.values():
  if len(candidates)!=1:continue
  target=candidates[0]
  if target['id']==passage['decision_id']:continue
  parts=re.split(r'(\s+|[-‑–])',target['az'])
  pattern=''.join(r'\s+' if p.isspace() else r'[-‑–]' if p in {'-','‑','–'} else re.escape(p) for p in parts)
  for match in re.finditer(r'(?<!\w)'+pattern+r'(?!\w)',text):
   # The mention must name its court before its docket. A number alone can
   # belong to an unrelated court or be quoted from another procedural record.
   prefix=text[max(0,match.start()-110):match.start()]
   aliases=[target['court_short'],target.get('court','')]
   if not any(a and re.search(r'(?<!\w)'+re.escape(a)+r'(?!\w)',prefix) for a in aliases):continue
   citations.append({'relation':'decision_citation','source_id':passage['decision_id'],'target_id':target['id'],
    'passage_id':passage['passage_id'],'source_role':passage['source_role'],'text_sha256':passage['text_sha256'],
    'start':match.start(),'end':match.end(),'quote':match.group(),'offset_unit':'unicode_codepoints',
    'treatment':'unclassified','binding_basis':'exact_docket_and_nearby_court_in_retained_text'})
 return sorted(citations,key=lambda c:(c['start'],c['target_id']))
