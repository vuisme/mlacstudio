from __future__ import annotations
import http.cookiejar,json,time,urllib.request
BASE='http://127.0.0.1:8733'; jar=http.cookiejar.CookieJar(); op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def req(path,method='GET',body=None,csrf=None):
 h={}; data=None
 if body is not None: data=json.dumps(body).encode(); h['Content-Type']='application/json'
 if csrf: h['X-CSRF-Token']=csrf
 with op.open(urllib.request.Request(BASE+path,data=data,headers=h,method=method),timeout=60) as r: return json.loads(r.read() or b'{}')
st=req('/api/auth/status'); csrf=st['csrf']
if not st['configured']: csrf=req('/api/setup','POST',{'username':'smoke','password':'Temporary-Smoke-Only-9274!','confirm_password':'Temporary-Smoke-Only-9274!'},csrf)['csrf']
elif not st['authenticated']: csrf=req('/api/login','POST',{'username':'smoke','password':'Temporary-Smoke-Only-9274!'},csrf)['csrf']
def wait(job):
 while True:
  q=req('/api/queue'); x=next((v for v in q if v['id']==job),None)
  if not x:return
  if x['status'] in ('failed','cancelled'):raise RuntimeError(x)
  time.sleep(1)
def render(seed):
 j=req('/api/render','POST',{'session':'session-1','prompt':'a red ceramic cup on a white background','width':512,'height':512,'steps':2,'seed':seed},csrf); wait(j['id'])
for seed in (9001,9002):
 render(seed); s=req('/api/config')['model']; print('job',seed,s)
 if seed==9001:first=s['pid']
 else: assert s['pid']==first,(first,s)
time.sleep(11); last=req('/api/config')['model']; print('after_idle',last); assert last['status']=='unloaded',last
print(json.dumps({'first_pid':first,'reused':True,'unloaded':True}))
