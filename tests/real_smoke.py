from __future__ import annotations
import http.cookiejar, json, os, sys, time, urllib.request
from pathlib import Path

BASE = os.environ.get('QIS_BASE','http://127.0.0.1:8731')
ROOT = Path(__file__).resolve().parents[1]
MODE = sys.argv[1] if len(sys.argv)>1 else 't2i'
jar=http.cookiejar.CookieJar(); op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def req(path, method='GET', body=None, csrf=None, headers=None):
    data=None
    h=dict(headers or {})
    if body is not None:
        data=json.dumps(body).encode(); h['Content-Type']='application/json'
    if csrf: h['X-CSRF-Token']=csrf
    r=op.open(urllib.request.Request(BASE+path,data=data,headers=h,method=method),timeout=30)
    raw=r.read(); return json.loads(raw) if raw else {}

status=req('/api/auth/status'); csrf=status['csrf']
if not status['configured']:
    setup=req('/api/setup','POST',{'username':'smoke-admin','password':'Temporary-Smoke-Only-9274!','confirm_password':'Temporary-Smoke-Only-9274!'},csrf)
    csrf=setup['csrf']
elif not status['authenticated']:
    login=req('/api/login','POST',{'username':'smoke-admin','password':'Temporary-Smoke-Only-9274!'},csrf); csrf=login['csrf']
else: csrf=status['csrf']
cfg=req('/api/config'); csrf=cfg['csrf']; session=cfg['active']
before={x['id'] for x in req('/api/takes?session='+urllib.parse.quote(session))}
if MODE=='i2i':
    source=Path(os.environ['QIS_SOURCE']); payload=source.read_bytes()
    headers={'X-Filename':urllib.parse.quote(source.name),'Content-Type':'application/octet-stream','Content-Length':str(len(payload)),'X-CSRF-Token':csrf}
    raw=op.open(urllib.request.Request(BASE+'/api/upload?session='+urllib.parse.quote(session),data=payload,headers=headers,method='POST'),timeout=30).read()
    up=json.loads(raw)
body={'session':session,'prompt':('A luminous pastel-pink crystal flower on a clean white background, product photography' if MODE=='t2i' else 'Change only the hair color to soft pastel pink. Preserve identity, pose, clothes and background.'),'width':512,'height':512,'steps':4 if MODE=='t2i' else 2,'seed':321}
job=req('/api/render','POST',body,csrf); ident=job['id']; start=time.time()
while True:
    q=req('/api/queue'); items=q if isinstance(q,list) else ((q.get('running') and [q['running']]) or q.get('queued',[]) or [])
    found=next((x for x in items if x.get('id')==ident),None)
    if not found:
        takes=req('/api/takes?session='+urllib.parse.quote(session))
        take=next((x for x in takes if x.get('id') not in before), None)
        if take:
            print(json.dumps({'mode':MODE,'seconds':round(time.time()-start,2),'job':ident,'take':take},ensure_ascii=False)); break
        raise RuntimeError('job disappeared without gallery result')
    if found.get('status') in {'failed','cancelled'}: raise RuntimeError(json.dumps(found))
    print(json.dumps({'status':found.get('status'),'progress':found.get('progress')}),flush=True); time.sleep(2)
