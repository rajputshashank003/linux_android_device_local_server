"""Private SQLite metadata, conversations, and local document chunks for MyForever."""
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(os.environ.get('MYFOREVER_DB_PATH', str(Path.home() / '.local' / 'share' / 'myforever' / 'platform.sqlite3'))).expanduser()
SCHEMA_VERSION = 2
OWNER_RE = re.compile(r'^[A-Za-z0-9_-]{32,64}$')
MAX_TITLE = 100
MAX_MESSAGE = 30000
MAX_MESSAGES = 1000


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect():
    try:
        from agent import ROOT as shared_root
        if DB_PATH.expanduser().resolve().is_relative_to(shared_root.resolve()):
            raise RuntimeError('The application database must be stored outside the public Shared directory.')
    except ImportError:
        pass
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try: os.chmod(DB_PATH.parent, 0o700)
    except OSError: pass
    db = sqlite3.connect(DB_PATH, timeout=5, isolation_level='DEFERRED', factory=_ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA journal_mode=WAL')
    try: os.chmod(DB_PATH, 0o600)
    except OSError: pass
    return db


def init():
    with _connect() as db:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError('MyForever database is newer than this server version.')
        if version < 1:
            db.executescript('''
                CREATE TABLE conversations (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX conversations_owner_updated ON conversations(owner_id, updated_at DESC);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
                    content TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL, metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX messages_conversation_id ON messages(conversation_id,id);
                CREATE TABLE documents (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, modified REAL NOT NULL,
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    indexed_at REAL NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE document_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL, page INTEGER, content TEXT NOT NULL,
                    UNIQUE(document_id, chunk_index)
                );
                CREATE INDEX chunks_document_id ON document_chunks(document_id,chunk_index);
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
                );
                PRAGMA user_version=1;
            ''')
        version=db.execute('PRAGMA user_version').fetchone()[0]
        if version < 2:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS indexing_jobs (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, total INTEGER NOT NULL,
                    done INTEGER NOT NULL, current TEXT NOT NULL DEFAULT '',
                    results TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS indexing_jobs_updated ON indexing_jobs(updated_at DESC);
                PRAGMA user_version=2;
            ''')
    return {'ok': True, 'schemaVersion': SCHEMA_VERSION}


def validate_owner(value):
    if not isinstance(value, str) or not OWNER_RE.fullmatch(value):
        raise ValueError('Invalid conversation owner.')
    return value


def new_owner_id():
    return secrets.token_urlsafe(32)


def _conversation(row):
    return {k: row[k] for k in ('id','title','model','created_at','updated_at')}


def create_conversation(owner, title='New chat', model=''):
    owner = validate_owner(owner)
    title = ' '.join(str(title).split())[:MAX_TITLE] or 'New chat'
    model = str(model)[:160]
    now = time.time(); cid = str(uuid.uuid4())
    with _connect() as db:
        count = db.execute('SELECT COUNT(*) FROM conversations WHERE owner_id=?',(owner,)).fetchone()[0]
        if count >= 200:
            raise ValueError('This browser has reached the 200-conversation limit. Delete an old conversation first.')
        db.execute('INSERT INTO conversations(id,owner_id,title,model,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   (cid,owner,title,model,now,now))
        row = db.execute('SELECT * FROM conversations WHERE id=?',(cid,)).fetchone()
    return _conversation(row)


def list_conversations(owner, limit=100):
    owner = validate_owner(owner); limit = max(1,min(int(limit),200))
    with _connect() as db:
        rows = db.execute('SELECT * FROM conversations WHERE owner_id=? ORDER BY updated_at DESC LIMIT ?',
                          (owner,limit)).fetchall()
        return [_conversation(row) for row in rows]


def get_conversation(owner, cid, limit=MAX_MESSAGES):
    owner = validate_owner(owner); limit=max(1,min(int(limit),MAX_MESSAGES))
    with _connect() as db:
        row=db.execute('SELECT * FROM conversations WHERE id=? AND owner_id=?',(str(cid),owner)).fetchone()
        if row is None: return None
        messages=db.execute('SELECT id,role,content,model,created_at,metadata FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?',
                            (str(cid),limit)).fetchall()
    result=_conversation(row)
    result['messages']=[{'id':m['id'],'role':m['role'],'content':m['content'],'model':m['model'],
                         'created_at':m['created_at'],'metadata':json.loads(m['metadata'] or '{}')}
                        for m in reversed(messages)]
    return result


def add_message(owner, cid, role, content, model='', metadata=None):
    owner=validate_owner(owner)
    if role not in ('user','assistant','tool'): raise ValueError('Invalid message role.')
    if not isinstance(content,str) or not content.strip() or len(content)>MAX_MESSAGE:
        raise ValueError('Message must contain 1 to 30,000 characters.')
    meta=json.dumps(metadata if isinstance(metadata,dict) else {},ensure_ascii=False)
    if len(meta)>4000: raise ValueError('Message metadata is too large.')
    now=time.time()
    with _connect() as db:
        row=db.execute('SELECT title FROM conversations WHERE id=? AND owner_id=?',(str(cid),owner)).fetchone()
        if row is None: return None
        db.execute('INSERT INTO messages(conversation_id,role,content,model,created_at,metadata) VALUES(?,?,?,?,?,?)',
                   (str(cid),role,content,str(model)[:160],now,meta))
        if role=='user' and row['title']=='New chat':
            title=' '.join(content.split())[:MAX_TITLE]
            db.execute('UPDATE conversations SET title=?,updated_at=? WHERE id=?',(title or 'New chat',now,str(cid)))
        else:
            db.execute('UPDATE conversations SET updated_at=?,model=CASE WHEN ?<>\'\' THEN ? ELSE model END WHERE id=?',
                       (now,str(model)[:160],str(model)[:160],str(cid)))
        message_id=db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.execute('''DELETE FROM messages WHERE conversation_id=? AND id NOT IN
          (SELECT id FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?)''',(str(cid),str(cid),MAX_MESSAGES))
    return {'id':message_id,'role':role,'content':content,'model':str(model)[:160],'created_at':now,'metadata':metadata or {}}


def rename_conversation(owner,cid,title):
    owner=validate_owner(owner); title=' '.join(str(title).split())[:MAX_TITLE]
    if not title: raise ValueError('Conversation title cannot be empty.')
    with _connect() as db:
        cur=db.execute('UPDATE conversations SET title=?,updated_at=? WHERE id=? AND owner_id=?',
                       (title,time.time(),str(cid),owner))
        return cur.rowcount>0


def delete_conversation(owner,cid):
    owner=validate_owner(owner)
    with _connect() as db:
        cur=db.execute('DELETE FROM conversations WHERE id=? AND owner_id=?',(str(cid),owner))
        return cur.rowcount>0


def upsert_document(path,name,sha256,size,modified,chunks,error=''):
    now=time.time(); status='ready' if chunks else 'error'
    with _connect() as db:
        old=db.execute('SELECT id,sha256,status,chunk_count FROM documents WHERE path=?',(path,)).fetchone()
        if old and old['sha256']==sha256 and old['status']=='ready':
            return {'unchanged':True,'id':old['id'],'chunks':old['chunk_count']}
        doc_id=old['id'] if old else secrets.token_hex(16)
        if old and error and not chunks:
            # Keep the last valid chunk set transactionally when re-extraction
            # fails. Mark it unusable for retrieval until a successful reindex.
            db.execute('''UPDATE documents SET name=?,sha256=?,size=?,modified=?,status='error',error=?,indexed_at=? WHERE id=?''',
              (name,sha256,int(size),float(modified),str(error)[:300],now,doc_id))
            return {'unchanged':False,'id':doc_id,'chunks':old['chunk_count'],'status':'error','error':str(error)[:300]}
        if old:
            db.execute('DELETE FROM document_chunks WHERE document_id=?',(doc_id,))
            db.execute('''UPDATE documents SET name=?,sha256=?,size=?,modified=?,status=?,error=?,indexed_at=?,chunk_count=? WHERE id=?''',
              (name,sha256,int(size),float(modified),status,str(error)[:300],now,len(chunks),doc_id))
        else:
            db.execute('''INSERT INTO documents(id,path,name,sha256,size,modified,status,error,indexed_at,chunk_count)
              VALUES(?,?,?,?,?,?,?,?,?,?)''',
              (doc_id,path,name,sha256,int(size),float(modified),status,str(error)[:300],now,len(chunks)))
        db.executemany('INSERT INTO document_chunks(document_id,chunk_index,page,content) VALUES(?,?,?,?)',
                       [(doc_id,i,page,text) for i,(page,text) in enumerate(chunks)])
    return {'unchanged':False,'id':doc_id,'chunks':len(chunks),'status':status,'error':str(error)[:300]}


def indexed_documents():
    with _connect() as db:
        return [dict(row) for row in db.execute('SELECT id,path,name,sha256,size,modified,status,error,indexed_at,chunk_count FROM documents ORDER BY indexed_at DESC')]


def remove_document(path):
    with _connect() as db:
        cur=db.execute('DELETE FROM documents WHERE path=?',(path,)); return cur.rowcount>0


def retrieve_chunks(query,limit=4,max_chars=6000):
    terms=list(dict.fromkeys(t.casefold() for t in re.findall(r'[\w-]{2,}',str(query))))[:16]
    if not terms:return []
    with _connect() as db:
        rows=db.execute('''SELECT d.path,d.name,c.page,c.content FROM document_chunks c
          JOIN documents d ON d.id=c.document_id WHERE d.status='ready' ORDER BY d.indexed_at DESC LIMIT 4000''').fetchall()
    ranked=[]
    for row in rows:
        text=row['content']; low=text.casefold()
        score=sum(low.count(t) for t in terms)
        if score: ranked.append((score,row))
    ranked.sort(key=lambda x:(-x[0],x[1]['path']))
    result=[]; used=0
    for _,row in ranked:
        value=row['content'][:max(0,max_chars-used)]
        if not value:break
        result.append({'path':row['path'],'name':row['name'],'page':row['page'],'text':value})
        used+=len(value)
        if len(result)>=max(1,min(limit,8)) or used>=max_chars:break
    return result



def save_index_job(job):
    job_id=str(job.get('id',''))
    if not re.fullmatch(r'[a-f0-9]{32}',job_id): raise ValueError('Invalid indexing job identifier.')
    status=job.get('status','running')
    if status not in ('running','complete','failed'): raise ValueError('Invalid indexing job state.')
    now=time.time()
    results=json.dumps(job.get('results',[])[:MAX_MESSAGES],ensure_ascii=False)
    query="""INSERT INTO indexing_jobs(id,status,total,done,current,results,error,started_at,updated_at)
      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,total=excluded.total,
      done=excluded.done,current=excluded.current,results=excluded.results,error=excluded.error,updated_at=excluded.updated_at"""
    with _connect() as db:
        db.execute(query,(job_id,status,max(0,int(job.get('total',0))),max(0,int(job.get('done',0))),
          str(job.get('current',''))[:500],results,str(job.get('error',''))[:300],float(job.get('startedAt') or now),now))


def last_index_job():
    with _connect() as db:
        row=db.execute('SELECT * FROM indexing_jobs ORDER BY updated_at DESC LIMIT 1').fetchone()
    if not row:return None
    return {'id':row['id'],'running':row['status']=='running','status':row['status'],'total':row['total'],
            'done':row['done'],'current':row['current'],'results':json.loads(row['results'] or '[]'),
            'error':row['error'],'startedAt':row['started_at'],'updatedAt':row['updated_at']}

def health():
    try:
        with _connect() as db:
            db.execute('SELECT 1').fetchone()
            conversations=db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
            messages=db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            docs=db.execute('SELECT COUNT(*) FROM documents WHERE status=\'ready\'').fetchone()[0]
            chunks=db.execute('SELECT COUNT(*) FROM document_chunks').fetchone()[0]
            jobs=db.execute('SELECT COUNT(*) FROM indexing_jobs').fetchone()[0]
        return {'state':'healthy','path':'private application storage','sizeBytes':DB_PATH.stat().st_size if DB_PATH.exists() else 0,
                'conversations':conversations,'messages':messages,'indexedDocuments':docs,'indexedChunks':chunks,'indexingJobs':jobs}
    except (OSError,sqlite3.Error,RuntimeError) as exc:
        return {'state':'unavailable','error':type(exc).__name__}
