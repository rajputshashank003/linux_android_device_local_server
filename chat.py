"""Authenticated, bounded, in-memory WebSocket chat for MyForever."""
import base64, hashlib, json, re, secrets, socket, struct, threading, time
from collections import deque
from datetime import datetime, timezone

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
MAX_FRAME, MAX_TEXT, MAX_CLIENTS, HISTORY_LIMIT = 4096, 500, 40, 60
RATE_WINDOW, RATE_COUNT = 8.0, 5
NAME_RE = re.compile(r'^[A-Za-z0-9_. -]{2,24}$')

def pack(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

class Hub:
    def __init__(self):
        self.lock = threading.RLock(); self.clients = set(); self.history = deque(maxlen=HISTORY_LIMIT)
    def add(self, client):
        with self.lock:
            if len(self.clients) >= MAX_CLIENTS: return None
            self.clients.add(client)
            return {'type':'init','online':len(self.clients),'history':list(self.history)}
    def broadcast(self, recipients, obj):
        payload = pack(obj)
        for peer in recipients:
            try: peer.send_frame(1, payload)
            except (OSError, ValueError): peer.close_socket()
    def remove(self, client):
        with self.lock:
            self.clients.discard(client); peers=tuple(self.clients); count=len(self.clients)
        self.broadcast(peers, {'type':'presence','online':count})
    def join(self, client, name):
        with self.lock:
            client.name=name; client.joined=True; peers=tuple(self.clients); count=len(self.clients)
        self.broadcast(peers, {'type':'presence','online':count})
    def message(self, client, text):
        now=time.monotonic(); client.times=[t for t in client.times if now-t<RATE_WINDOW]
        if len(client.times)>=RATE_COUNT:
            client.send_json({'type':'error','message':'Please slow down.'}); return
        client.times.append(now)
        text=' '.join(''.join(ch for ch in text if ch in '\n\t' or (ord(ch)>=32 and ord(ch)!=127)).split())
        if not text or len(text)>MAX_TEXT:
            client.send_json({'type':'error','message':'Message must be 1–500 characters.'}); return
        with self.lock:
            item={'type':'message','id':secrets.token_hex(6),'name':client.name,'text':text,
                  'time':datetime.now(timezone.utc).isoformat(timespec='seconds')}
            self.history.append(item); peers=tuple(self.clients)
        self.broadcast(peers,item)

HUB=Hub()

class WSClient:
    def __init__(self, handler):
        self.sock=handler.connection; self.lock=threading.Lock(); self.closed=False
        self.name='Guest'; self.joined=False; self.times=[]
    def send_frame(self, opcode, payload=b''):
        if self.closed: raise OSError('socket closed')
        n=len(payload)
        head=bytes((0x80|opcode,n)) if n<126 else bytes((0x80|opcode,126))+struct.pack('!H',n) if n<65536 else bytes((0x80|opcode,127))+struct.pack('!Q',n)
        with self.lock: self.sock.sendall(head+payload)
    def send_json(self,obj): self.send_frame(1,pack(obj))
    def close_socket(self):
        self.closed=True
        try: self.sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: self.sock.close()
        except OSError: pass
    def exact(self,n):
        data=bytearray()
        while len(data)<n:
            part=self.sock.recv(n-len(data))
            if not part: raise EOFError
            data.extend(part)
        return bytes(data)
    def read_frame(self):
        a,b=self.exact(2)
        if a&0x70 or not a&0x80 or not b&0x80: raise ValueError('invalid frame')
        opcode=a&15; size=b&127
        if size==126: size=struct.unpack('!H',self.exact(2))[0]
        elif size==127: size=struct.unpack('!Q',self.exact(8))[0]
        if size>MAX_FRAME or (opcode>=8 and size>125): raise ValueError('frame too large')
        mask=self.exact(4); data=bytearray(self.exact(size))
        for i in range(size): data[i]^=mask[i%4]
        return opcode,bytes(data)

def handle_websocket(handler):
    """Called only after FileHandler.authenticate() succeeds."""
    from urllib.parse import urlsplit
    handler.protocol_version='HTTP/1.1'
    try:
        origin=urlsplit(handler.headers.get('Origin','')); origin_host=origin.netloc.lower()
        good_origin=origin.scheme in ('http','https') and origin_host==handler.headers.get('Host','').lower()
    except ValueError: good_origin=False
    key=handler.headers.get('Sec-WebSocket-Key','')
    try: good_key=len(base64.b64decode(key,validate=True))==16
    except (ValueError,base64.binascii.Error): good_key=False
    connection={v.strip().lower() for v in handler.headers.get('Connection','').split(',')}
    if (handler.command!='GET' or handler.headers.get('Upgrade','').lower()!='websocket'
        or 'upgrade' not in connection or handler.headers.get('Sec-WebSocket-Version')!='13'
        or not good_key or not good_origin):
        handler.reply(400,b'Invalid WebSocket upgrade'); return
    client=WSClient(handler); initial=HUB.add(client)
    if initial is None: handler.reply(503,b'Chat is full'); return
    accept=base64.b64encode(hashlib.sha1((key+GUID).encode('ascii')).digest()).decode('ascii')
    try:
        handler.send_response(101,'Switching Protocols')
        for name,value in [('Upgrade','websocket'),('Connection','Upgrade'),('Sec-WebSocket-Accept',accept),('Cache-Control','no-store')]: handler.send_header(name,value)
        handler.end_headers(); handler.wfile.flush(); client.send_json(initial)
        while True:
            opcode,payload=client.read_frame()
            if opcode==8:
                try: client.send_frame(8,payload[:125])
                except OSError: pass
                break
            if opcode==9: client.send_frame(10,payload); continue
            if opcode!=1: raise ValueError('unsupported opcode')
            try: data=json.loads(payload.decode('utf-8'))
            except (UnicodeError,json.JSONDecodeError): client.send_json({'type':'error','message':'Invalid chat message.'}); continue
            if not isinstance(data,dict): continue
            if not client.joined and data.get('type')=='join':
                name=data.get('username')
                if not isinstance(name,str) or not NAME_RE.fullmatch(name.strip()):
                    client.send_json({'type':'error','message':'Name must be 2–24 letters, numbers, spaces, dots, _ or -.'}); continue
                HUB.join(client,name.strip()); continue
            text=data.get('text')
            if client.joined and data.get('type')=='message' and isinstance(text,str): HUB.message(client,text)
    except (EOFError,OSError,ValueError): pass
    finally:
        HUB.remove(client); client.close_socket(); handler.close_connection=True
