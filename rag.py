"""Small local lexical RAG index stored in the private SQLite application database."""
import hashlib, os, threading, time
from pathlib import Path
import app_db

MAX_FILES_PER_JOB=20
MAX_TEXT_BYTES=512*1024
MAX_PDF_BYTES=8*1024*1024
CHUNK_CHARS=1400
_job_lock=threading.RLock()
_job={'running':False,'id':'','total':0,'done':0,'current':'','results':[],'startedAt':None,'error':''}
_worker_lock=threading.Lock()


def _root():
    from agent import ROOT
    return ROOT.resolve(strict=True)


def _safe_target(relative):
    if not isinstance(relative,str) or not relative or relative.startswith('/') or '\\' in relative:
        raise ValueError('Choose files from Shared.')
    parts=relative.split('/')
    if any(p in ('','.','..') for p in parts): raise ValueError('Invalid Shared file path.')
    root=_root(); target=root.joinpath(*parts); probe=root
    for part in parts:
        probe=probe/part
        if probe.is_symlink(): raise ValueError('Symbolic links cannot be indexed.')
    resolved=target.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file(): raise ValueError('Selected file is unavailable.')
    return resolved


def _extract(path):
    ext=path.suffix.lower(); limit=MAX_PDF_BYTES if ext=='.pdf' else MAX_TEXT_BYTES
    size=path.stat().st_size
    if size>limit: raise ValueError('File exceeds the indexing size limit.')
    if ext=='.pdf':
        try: from pypdf import PdfReader
        except ImportError: raise ValueError('PDF indexing requires the existing optional pypdf package.')
        pages=[]
        try:
            reader=PdfReader(str(path),strict=False)
            for i,page in enumerate(reader.pages[:100],1):
                text=(page.extract_text() or '').strip()
                if text: pages.append((i,text))
        except Exception: raise ValueError('PDF text could not be extracted.')
        if not pages: raise ValueError('No text was extracted. Scanned PDFs require local OCR.')
        result=[]
        for page,text in pages:
            for start in range(0,len(text),CHUNK_CHARS):
                result.append((page,text[start:start+CHUNK_CHARS]))
        return result
    if ext not in {'.txt','.md','.markdown','.csv','.tsv','.json','.yaml','.yml','.toml','.ini','.py','.js','.ts','.tsx','.jsx','.html','.htm','.css','.xml','.log','.sql','.sh','.rst','.tex','.java','.kt','.c','.h','.cpp','.go','.rs','.swift','.rb','.php','.ipynb'}:
        raise ValueError('This file type is not supported for local indexing.')
    text=path.read_bytes().decode('utf-8-sig','replace')
    if not text.strip(): raise ValueError('No text was found in this file.')
    return [(None,text[i:i+CHUNK_CHARS]) for i in range(0,min(len(text),400_000),CHUNK_CHARS)]


def _index_worker(job_id,paths):
    try:
        for relative in paths:
            with _job_lock: _job['current']=relative
            try:
                path=_safe_target(relative); stat=path.stat(); digest=hashlib.sha256()
                with path.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
                chunks=_extract(path)
                result=app_db.upsert_document(relative,path.name,digest.hexdigest(),stat.st_size,stat.st_mtime,chunks)
                state='unchanged' if result.get('unchanged') else 'indexed'
                entry={'path':relative,'status':state,'chunks':result.get('chunks',0)}
            except Exception as exc:
                try:
                    path=_safe_target(relative); st=path.stat(); app_db.upsert_document(relative,path.name,'',st.st_size,st.st_mtime,[],str(exc))
                except Exception: pass
                entry={'path':relative,'status':'failed','error':str(exc)[:240]}
            with _job_lock:
                _job['results'].append(entry);_job['done']+=1
                try:app_db.save_index_job(_job)
                except Exception:pass
    finally:
        with _job_lock:
            _job.update(running=False,current='')
            if _job.get('id')==job_id:_job['status']='complete' if not any(x.get('status')=='failed' for x in _job['results']) else 'failed'
            try:app_db.save_index_job(_job)
            except Exception:pass
        _worker_lock.release()


def start(paths):
    if not isinstance(paths,list) or not paths or len(paths)>MAX_FILES_PER_JOB or any(not isinstance(x,str) for x in paths):
        raise ValueError('Select between 1 and 20 files for one indexing job.')
    unique=list(dict.fromkeys(paths))
    if len(unique)!=len(paths): raise ValueError('Duplicate file selections are not allowed.')
    for rel in unique: _safe_target(rel)
    if not _worker_lock.acquire(blocking=False): raise RuntimeError('An indexing job is already running.')
    import uuid
    job_id=uuid.uuid4().hex
    with _job_lock:_job.update(running=True,id=job_id,total=len(unique),done=0,current='',results=[],startedAt=time.time(),error='')
    _job['status']='running'
    app_db.save_index_job(_job)
    threading.Thread(target=_index_worker,args=(job_id,unique),name='myforever-rag-index',daemon=True).start()
    return {'started':True,'id':job_id,'total':len(unique)}


def status():
    with _job_lock:
        job=dict(_job); job['results']=list(_job['results'])
        running_here=bool(_job.get('running'))
    try:
        docs=app_db.indexed_documents()
        if not running_here:
            persisted=app_db.last_index_job()
            if persisted:
                job=persisted
                if job.get('running'):
                    job.update(running=False,status='failed',current='',error='Indexing stopped before the server restarted. Retry the selected files.')
                    app_db.save_index_job(job)
    except Exception as exc:return {'job':job,'documents':[],'state':'unavailable','error':type(exc).__name__}
    return {'job':job,'documents':docs,'state':'healthy'}


def retrieve(query,limit=4,max_chars=6000):
    return app_db.retrieve_chunks(query,limit,max_chars)


def remove(relative):
    if not isinstance(relative,str) or not relative or relative.startswith('/') or '\\' in relative or any(part in ('','.','..') for part in relative.split('/')):
        raise ValueError('Invalid indexed-file path.')
    return app_db.remove_document(relative)
