import base64,hashlib,hmac,http.server,json,os,pathlib,subprocess,threading,time,uuid
from playwright.sync_api import sync_playwright
ROOT=pathlib.Path(__file__).resolve().parent
NATIVE=os.environ['LIVEKIT_INTEROP_TEST_BINARY']
URL=os.environ.get('LIVEKIT_URL', 'ws://127.0.0.1:17880')
from urllib.parse import urlparse
assert urlparse(URL).hostname in ('localhost','127.0.0.1','::1'), 'This harness only uses a local development SFU'
SDK=pathlib.Path(os.environ.get('LIVEKIT_JS_DIST', str(ROOT.parents[1]/'frontend/node_modules/livekit-client/dist')))
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  file={'/':ROOT/'interop.html','/sdk.mjs':SDK/'livekit-client.esm.mjs','/worker.mjs':SDK/'livekit-client.e2ee.worker.mjs'}.get(self.path)
  if not file: self.send_error(404);return
  data=file.read_bytes();self.send_response(200);self.send_header('Content-Type','text/html' if self.path=='/' else 'application/javascript');self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',18889),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
def jwt(room):
 enc=lambda obj:base64.urlsafe_b64encode(json.dumps(obj,separators=(',',':')).encode()).rstrip(b'=')
 data=enc({'alg':'HS256','typ':'JWT'})+b'.'+enc({'iss':'devkey','sub':'browser','exp':int(time.time())+600,'video':{'roomJoin':True,'room':room}})
 return (data+b'.'+base64.urlsafe_b64encode(hmac.new(b'secret',data,hashlib.sha256).digest()).rstrip(b'=')).decode()
try:
 with sync_playwright() as p:
  browser=p.chromium.launch(args=['--no-sandbox','--autoplay-policy=no-user-gesture-required'])
  for role,backup in [('publisher',None),('receiver',None),('receiver','vp8')]:
   room='av1-interop-'+uuid.uuid4().hex
   env=dict(os.environ,INTEROP_ROLE=role,INTEROP_ROOM=room,LIVEKIT_URL=URL,RUST_LOG='info')
   if backup: env['INTEROP_BACKUP']=backup
   with open(pathlib.Path(os.environ.get('TMPDIR','/tmp'))/f'kaede-native-{role}-{backup}.log','w') as log:
    native=subprocess.Popen([NATIVE,'browser_encryption_interop','--ignored','--nocapture'],stdout=log,stderr=subprocess.STDOUT,env=env)
    page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('console',lambda m:print('browser',m.type,m.text[:250],flush=True) if m.type=='error' else None)
    try:
     page.goto('http://127.0.0.1:18889');page.wait_for_function('!!window.start')
     page.evaluate('([url, token, role, backup]) => window.start(url, token, role, backup)', [URL,jwt(room),'receiver' if role=='publisher' else 'publisher',backup])
     if role=='publisher':
      page.wait_for_function('window.decoded?.frames > 0',timeout=30000)
      result=page.evaluate('window.decoded');assert result['codec'].lower()=='video/av1',result
      print('native -> browser',result,flush=True)
     deadline=time.monotonic()+35
     while native.poll() is None and time.monotonic()<deadline: page.wait_for_timeout(100)
     code=native.wait(timeout=1);assert code==0,(role,code,errors)
     print('interop',role,backup,'PASS',flush=True)
    finally:
     native.terminate() if native.poll() is None else None
     page.close()
  browser.close()
finally:server.shutdown()
