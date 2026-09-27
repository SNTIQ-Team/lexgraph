"""Source-specific readers for retained court publications; never infer Rn. numbers.

The capture is a byte-for-byte public source, not a curated abstract. Derived text
has a locator into that source; another representation is not a second witness.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup, NavigableString, Tag

PROFILES={'bsg-html/1':('www.bsg.bund.de','text/html'),
          'cellar-xhtml/1':('publications.europa.eu','application/xhtml+xml'),
          'sozialgerichtsbarkeit-json/1':('www.sozialgerichtsbarkeit.de','application/json')}
MONTHS=['Januar','Februar','März','April','Mai','Juni','Juli','August','September','Oktober','November','Dezember']
def sha(data):return hashlib.sha256(data).hexdigest()
def norm(value):return ' '.join(str(value).replace('\u2011','-').replace('\u00ad','').split())
def text_content(node):
 def walk(el):
  if isinstance(el,NavigableString):return str(el)
  text=''.join(walk(c) for c in el.children)
  if el.name in {'p','div','h1','h2','h3','h4','tr','br','li','dd','dl'}:text+='\n'
  elif el.name in {'td','th'}:text+='\t'
  return text
 return '\n'.join('\t'.join(' '.join(cell.split()) for cell in line.split('\t')).strip() for line in walk(node).splitlines() if line.strip()).strip()

def read_capture(cache:Path,metadata:dict):
 file=cache/(metadata['id']+'.json')
 if not file.exists():return None
 record=json.loads(file.read_text());profile=record.get('profile')
 if profile not in PROFILES or record.get('decision_id')!=metadata['id']:raise ValueError('capture identity/profile mismatch')
 url=urlsplit(record.get('url',''));host,media=PROFILES[profile]
 if url.scheme!='https' or url.hostname!=host or url.username or url.password or url.port not in {None,443}:raise ValueError('capture not from expected official origin')
 if record.get('media_type')!=media:raise ValueError('capture media type mismatch')
 if datetime.fromisoformat(record.get('observed_at','')).tzinfo is None:raise ValueError('observation time requires timezone')
 digest=record.get('sha256','')
 if not re.fullmatch('[a-f0-9]{64}',digest):raise ValueError('invalid source hash')
 blob=(cache/(digest+'.source')).read_bytes()
 if sha(blob)!=digest:raise ValueError('capture bytes changed')
 return blob,record

def extract_official(blob:bytes,metadata:dict,profile:str):
 if len(blob)>8*1024*1024 or re.search(br'<!ENTITY\b',blob,re.I):raise ValueError('unsafe or oversized source')
 raw=blob.decode('utf-8',errors='strict')
 # XML validation prevents a tolerant HTML parser repairing changed numbered
 # source structure. External DTDs are neither downloaded nor expanded.
 if profile=='cellar-xhtml/1':
  import xml.etree.ElementTree as ET
  root=ET.fromstring(re.sub(r'<!DOCTYPE\b.*?>','',raw,flags=re.I|re.S))
  if root.tag!='{http://www.w3.org/1999/xhtml}html':raise ValueError('unexpected XHTML root')
 soup=BeautifulSoup(raw,'html.parser');rows=[];counts={}
 roles={'titelzeile':'published_title','tenor':'court_disposition','tatbestand':'court_reported_facts','entscheidungsgruende':'court_reasoning_section','sonstlt':'published_other','leitsatz':'published_headnote','gruende':'court_reasons_section'}
 def add(node,section,locator,*,anchor=None,label=None):
  text=text_content(node)
  if not text:return
  counts[section]=counts.get(section,0)+1;ordinal=counts[section]
  rows.append({'decision_id':metadata['id'],'passage_id':f'{section}:{anchor or "block-"+str(ordinal).zfill(4)}',
   'section':section,'ordinal':ordinal,'document_position':len(rows)+1,'source_role':roles[section],
   'source_anchor':anchor,'source_label':label,'paragraph_label':label,
   'source_locator':locator,'locator_type':'css','text':text,'text_sha256':sha(text.encode())})
 def direct(node):
  for child in node.children:
   if isinstance(child,NavigableString):
    if child.strip():raise ValueError('unrepresented source text')
   elif isinstance(child,Tag):yield child
 if profile=='bsg-html/1':
  headlines=soup.select('h1.entscheidungsHeadline')
  if len(headlines)!=1:raise ValueError('missing/ambiguous court decision heading')
  h=headlines[0];container=h.parent;heading=norm(h.get_text(' ',strip=True));date=datetime.fromisoformat(metadata['date']).strftime('%d.%m.%Y')
  kind={"Vorlagebeschluss":"Beschluss"}.get(metadata["kind"],metadata["kind"])
  expected=f"Bundessozialgericht {kind} vom {date} , {metadata['az']}"
  if norm(heading)!=norm(expected) or metadata['court_short']!='BSG':raise ValueError('decision identity mismatch')
  identity=container.get('id','')
  if not re.fullmatch('readme_[0-9]+',identity):raise ValueError('unexpected BSG source container')
  section='titelzeile';sections={'Tenor':'tenor','Tatbestand':'tatbestand','Entscheidungsgründe':'entscheidungsgruende'}
  def visit(el,prefix):
   nonlocal section
   tags={}
   for child in direct(el):
    tags[child.name]=tags.get(child.name,0)+1;locator=f'{prefix} > {child.name}:nth-of-type({tags[child.name]})'
    if child.name=='h2':
     section=sections.get(child.get_text(' ',strip=True))
     if section is None:raise ValueError('unknown court section')
    elif child.name=='div' and child.get('class')==['absatz']:visit(child,locator)
    elif child.name in {'h1','p'}:
     if child.get('id') or child.select('[id], [name]'):raise ValueError('new source anchor structure requires explicit reader')
     add(child,section,locator)
    else:raise ValueError('unsupported BSG block')
  visit(container,'#'+identity)
  if not {'tenor','entscheidungsgruende'}<=set(counts):raise ValueError('incomplete court text')
 elif profile=='sozialgerichtsbarkeit-json/1':
  import html
  source=json.loads(raw);court=source.get('gericht') or {};level=court.get('instanz');date_field={'SG':'datum_1_instanz','LSG':'datum_2_instanz','BSG':'datum_3_instanz'}.get(level)
  if not date_field or court.get('bezeichnung')!=metadata['court'] or source.get('aktenzeichen')!=metadata['az'] or source.get(date_field)!=metadata['date'] or source.get('kategorie')!='B' or metadata['kind'] not in {'Beschluss','Vorlagebeschluss'}:raise ValueError('portal decision identity mismatch')
  text=source.get('text')
  if not isinstance(text,str) or not text:raise ValueError('missing official portal text')
  body=BeautifulSoup(html.unescape(text),'html.parser');section='tenor';tags={}
  if source.get('leitsaetze'):
   node=BeautifulSoup(html.unescape(source['leitsaetze']),'html.parser');add(node,'leitsatz','/leitsaetze');rows[-1]['locator_type']='json_html_css'
  for child in direct(body):
   tags[child.name]=tags.get(child.name,0)+1
   if child.name!='p' or child.select('[id], [name], script, style') or child.get('id'):raise ValueError('unsupported portal paragraph structure')
   if re.sub(r'\s+','',child.get_text()) in {'Gründe:','Gründe'}:section='gruende'
   if text_content(child):
    add(child,section,f'/text :: p:nth-of-type({tags[child.name]})');rows[-1]['locator_type']='json_html_css'
  if not {'tenor','gruende'}<=set(counts):raise ValueError('incomplete portal decision')
 elif profile=='cellar-xhtml/1':
  if not soup.title or soup.title.get_text(strip=True)!=metadata.get('celex'):raise ValueError('CELEX identity mismatch')
  body=soup.body
  if body is None:raise ValueError('missing decision body')
  leading=' '.join(norm(x.get_text(' ',strip=True)) for x in list(body.find_all(recursive=False))[:6])
  date=datetime.fromisoformat(metadata['date']);expected_date=f'{date.day}. {MONTHS[date.month-1]} {date.year}'
  if metadata['court_short']!='EuGH' or metadata['kind']!='Urteil' or 'URTEIL DES GERICHTSHOFS' not in leading or expected_date not in leading or f"Rechtssache {norm(metadata['az'])}" not in leading:raise ValueError('decision identity mismatch')
  section='sonstlt';tags={};numbers=[]
  for child in direct(body):
   tags[child.name]=tags.get(child.name,0)+1;locator=f'body > {child.name}:nth-of-type({tags[child.name]})'
   markers=child.select('[id^=point]')
   if markers:
    if child.name!='table' or len(markers)!=1:raise ValueError('unexpected source paragraph structure')
    marker=markers[0];anchor=marker.get('id');label=marker.get_text(strip=True)
    if not re.fullmatch(r'\d+',label) or anchor!='point'+label:raise ValueError('paragraph marker mismatch')
    tr=child.find('tr');tds=tr.find_all('td',recursive=False)
    if len(tds)!=2 or marker.parent!=tds[0] or text_content(tds[0])!=label:raise ValueError('unexpected paragraph layout')
    numbers.append(int(label));add(tds[1],'entscheidungsgruende',f'#{anchor} parent::td following-sibling::td',anchor=anchor,label=label)
    rows[-1]['locator_type']='source_anchor_adjacent_cell'
   else:
    if 'Aus diesen Gründen hat der Gerichtshof' in child.get_text(' ',strip=True):section='tenor'
    if child.name not in {'p','table','hr'}:raise ValueError('unsupported XHTML block')
    if child.name=='hr':section='sonstlt';continue
    add(child,section,locator)
  if not numbers or numbers!=list(range(1,len(numbers)+1)) or 'tenor' not in counts:raise ValueError('missing or duplicate source paragraph')
 else:raise ValueError('unsupported source profile')
 if len({r['passage_id'] for r in rows})!=len(rows):raise ValueError('duplicate passage identity')
 eclis=set(re.findall(r'ECLI:[A-Z]{2}:[A-Z]+:[0-9]{4}:[A-Za-z0-9.]+',raw))
 ecli=next(iter(eclis)) if len(eclis)==1 else None
 return rows,ecli
