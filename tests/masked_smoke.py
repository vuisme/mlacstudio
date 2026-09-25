from __future__ import annotations
import http.cookiejar, json, os, time, urllib.parse, urllib.request
from pathlib import Path
from PIL import Image, ImageDraw, ImageChops
BASE=os.environ.get('QIS_BASE','http://127.0.0.1:8732'); source=Path(os.environ['QIS_SOURCE'])
jar=http.cookiejar.CookieJar(); op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def req(path,method='GET',body=None,csrf=None):
 h={}; data=None
 if body is not None: data=json.dumps(body).encode(); h['Content-Type']='application/json'
 if csrf: h['X-CSRF-Token']=csrf
 r=op.open(urllib.request.Request(BASE+path,data=data,headers=h,method=method),timeout=30); b=r.read(); return json.loads(b) if b else {}
def rawpost(path,payload,name,csrf,content_type='application/octet-stream',extra=None):
 h={'X-Filename':urllib.parse.quote(name),'Content-Type':content_type,'Content-Length':str(len(payload)),'X-CSRF-Token':csrf}
 h.update(extra or {})
 try:
  return json.loads(op.open(urllib.request.Request(BASE+path,data=payload,headers=h,method='POST'),timeout=30).read())
 except urllib.error.HTTPError as exc:
  raise RuntimeError(f'{exc.code}: {exc.read().decode()}') from exc
st=req('/api/auth/status'); csrf=st['csrf']
if not st['configured']: csrf=req('/api/setup','POST',{'username':'smoke-admin','password':'Temporary-Smoke-Only-9274!','confirm_password':'Temporary-Smoke-Only-9274!'},csrf)['csrf']
elif not st['authenticated']: csrf=req('/api/login','POST',{'username':'smoke-admin','password':'Temporary-Smoke-Only-9274!'},csrf)['csrf']
else: csrf=st['csrf']
cfg=req('/api/config'); csrf=cfg['csrf']; session=cfg['active']
rawpost('/api/upload?session='+urllib.parse.quote(session),source.read_bytes(),source.name,csrf)
with Image.open(source) as im: w,h=im.size
mask=Image.new('L',(w,h),0); d=ImageDraw.Draw(mask); d.ellipse((w//3,h//3,2*w//3,2*h//3),fill=255)
tmp=source.parent/'masked-smoke-mask.png'; mask.save(tmp); rawpost('/api/mask?session='+urllib.parse.quote(session),tmp.read_bytes(),tmp.name,csrf,'image/png',{'X-Mask-Feather':'0'})
before={x['id'] for x in req('/api/takes?session='+urllib.parse.quote(session))}
job=req('/api/render','POST',{'session':session,'prompt':'Change only the center object to vivid red, preserve everything outside the painted selection','width':512,'height':512,'steps':2,'seed':777},csrf); ident=job['id']; start=time.time()
while True:
 q=req('/api/queue'); found=next((x for x in q if x.get('id')==ident),None)
 if not found:
  take=next((x for x in req('/api/takes?session='+urllib.parse.quote(session)) if x['id'] not in before),None)
  if not take: raise RuntimeError('job disappeared')
  out=(ROOT/'data-mask-smoke-2'/'gallery'/(take['id']+'.png'));
  with Image.open(source).convert('RGB') as a, Image.open(out).convert('RGB') as b:
   b=b.resize(a.size); outside=ImageChops.difference(a,b); outside.putalpha(Image.eval(mask,lambda x:255-x)); bbox=outside.getbbox()
  print(json.dumps({'seconds':round(time.time()-start,2),'take':take,'outside_mask_difference_bbox':bbox},ensure_ascii=False)); break
 if found.get('status') in {'failed','cancelled'}: raise RuntimeError(json.dumps(found))
 time.sleep(2)
