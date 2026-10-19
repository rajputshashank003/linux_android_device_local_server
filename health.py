"""Authenticated, real local health and resource snapshot for MyForever."""
import os, re, shutil, socket, subprocess, time
from pathlib import Path
from urllib.request import urlopen
from urllib.error import URLError

STARTED = time.monotonic()
_cpu_lock = __import__('threading').Lock()
_cpu_sample = None
_process_cpu_sample = None
_cpu_unavailable_reason = None


def _meminfo():
    result={}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key,_,rest=line.partition(':')
            if key in ('MemTotal','MemAvailable','MemFree','Buffers','Cached','SwapTotal','SwapFree'):
                result[key]=int(rest.split()[0])*1024
    except (OSError,ValueError,IndexError): pass
    return result


def _cpu():
    """System-wide CPU utilization from /proc/stat deltas; unsupported if counters are denied."""
    global _cpu_sample, _cpu_unavailable_reason
    try:
        fields=Path('/proc/stat').read_text().splitlines()[0].split()[1:]
        vals=[int(v) for v in fields]
        idle=vals[3]+(vals[4] if len(vals)>4 else 0)
        total=sum(vals)
    except (OSError,ValueError,IndexError) as exc:
        # Toybox top's first interval reports cumulative-looking aggregates that
        # can disagree with per-process CPU on Android. Do not turn that into a
        # false 0%; report system CPU unavailable and expose measured process CPU.
        _cpu_unavailable_reason = f"/proc/stat unavailable ({type(exc).__name__}); no reliable system-wide fallback"
        return None
    now=time.monotonic()
    with _cpu_lock:
        previous=_cpu_sample; _cpu_sample=(total,idle,now)
    if previous is None:return None
    dt=total-previous[0]
    return round(max(0,min(100,(dt-(idle-previous[1]))*100/dt)),1) if dt>0 else None


def _process_cpu():
    """Process CPU as percent of the whole machine, derived from /proc/self/stat deltas."""
    global _process_cpu_sample
    try:
        raw = Path('/proc/self/stat').read_text()
        fields = raw[raw.rfind(')') + 2:].split()
        utime, stime = int(fields[11]), int(fields[12])
        hz = os.sysconf('SC_CLK_TCK')
        cores = max(1, os.cpu_count() or 1)
        now = time.monotonic()
        with _cpu_lock:
            previous = _process_cpu_sample
            _process_cpu_sample = (utime + stime, now)
        if previous is None: return None
        elapsed = now - previous[1]
        ticks = (utime + stime) - previous[0]
        if elapsed <= 0 or ticks < 0: return None
        return round(max(0.0, min(100.0, ticks / hz / elapsed / cores * 100)), 1)
    except (OSError, ValueError, IndexError, TypeError, AttributeError):
        return None


def _storage_details(shared, local_ai):
    root = shared if shared.exists() else Path(__file__).resolve().parent
    try:
        disk = shutil.disk_usage(root)
        storage = {'state': 'healthy', 'totalBytes': disk.total, 'usedBytes': disk.used,
                   'freeBytes': disk.free, 'usedPercent': round(disk.used * 100 / disk.total, 1) if disk.total else None}
    except OSError as exc:
        storage = {'state': 'unavailable', 'error': type(exc).__name__}
    model_bytes = 0
    temporary_bytes = 0
    try:
        for model_id in local_ai.MODELS:
            _, model_file, marker, part = local_ai._paths(model_id)
            if model_file.is_file() and not model_file.is_symlink(): model_bytes += model_file.stat().st_size
            for temp in (marker.with_suffix('.tmp'), part.with_suffix('.part.tmp')):
                if temp.is_file() and not temp.is_symlink(): temporary_bytes += temp.stat().st_size
    except (OSError, ValueError):
        model_bytes = None
    storage['modelsBytes'] = model_bytes
    storage['temporaryBytes'] = temporary_bytes
    storage['modelDirectoryFreeBytes'] = shutil.disk_usage(local_ai.MODEL_ROOT).free if local_ai.MODEL_ROOT.exists() else None
    return storage


def _process_rss():
    try:
        for line in Path('/proc/self/status').read_text().splitlines():
            if line.startswith('VmRSS:'):return int(line.split()[1])*1024
    except (OSError,ValueError,IndexError):pass
    return None


def _tunnel():
    try:
        with urlopen('http://127.0.0.1:20241/ready',timeout=.7) as r:
            import json
            data=json.loads(r.read(2048))
            count=int(data.get('readyConnections',0))
            return {'state':'healthy' if r.status==200 and count else 'degraded','readyConnections':count}
    except Exception:return {'state':'unavailable','readyConnections':0}


def _probe_openrouter():
    try:
        from agent import _api_key
        key=_api_key()
        if not key:return {'state':'unconfigured'}
        from urllib.request import Request
        req=Request('https://openrouter.ai/api/v1/models',headers={'Authorization':'Bearer '+key,'User-Agent':'MyForever-health-check/1.0'})
        with urlopen(req,timeout=3) as r:return {'state':'healthy' if r.status==200 else 'degraded','httpStatus':r.status}
    except Exception as exc:return {'state':'unavailable','error':type(exc).__name__}


def snapshot(probe_external=False):
    started = time.monotonic()
    import app_db
    import local_ai
    mem=_meminfo()
    try:
        from agent import ROOT, _INDEX
        shared=ROOT
        index_state={'state':'ready' if _INDEX.get('built') else 'not_built','cachedDocuments':len(_INDEX.get('rows',[])),
                     'cachedErrors':len(_INDEX.get('errors',[]))}
    except Exception:
        shared=Path(__file__).resolve().parent/'shared';index_state={'state':'unavailable','cachedDocuments':None}
    storage = _storage_details(shared, local_ai)
    try:
        st=app_db.health()
    except Exception as exc:st={'state':'unavailable','error':type(exc).__name__}
    try:
        model=local_ai.status()
        model_state='healthy' if model.get('inferenceReady') else ('degraded' if model.get('installed') else 'stopped')
        model_info={'state':model_state,'activeModel':model.get('activeModel') or None,'installed':model.get('installed'),
                    'loaded':model.get('loaded'),'runtimeAvailable':model.get('engineAvailable'),'inferenceBusy':bool(local_ai._active_job.locked()),'inferenceProcessRssBytes':(model.get('inferenceProcessRssMb')*1048576 if model.get('inferenceProcessRssMb') is not None else None),
                    'availableMemoryBytes':(model.get('availableMemoryMb')*1048576 if model.get('availableMemoryMb') is not None else None),
                    'modelMemoryEstimateMb':model.get('estimatedMemoryMb'),'loadState':model.get('loadState'),'lastLoadError':model.get('lastLoadError'),'runtimeSettings':model.get('runtimeSettings'),'lastLoad':model.get('lastLoad'),'lastInference':model.get('lastInference'),'modelFileBytes':next((item.get('sizeBytes') for item in model.get('models',[]) if item.get('id')==model.get('activeModel')),None)}
    except Exception as exc:model_info={'state':'unavailable','error':type(exc).__name__}
    try:
        from agent import status as agent_status
        openrouter={'state':'configured' if agent_status().get('configured') else 'unconfigured'}
        if probe_external and openrouter['state']=='configured':openrouter=_probe_openrouter()
    except Exception as exc:openrouter={'state':'unavailable','error':type(exc).__name__}
    try:
        import rag
        rag_state=rag.status()
        index_state={**index_state,'job':rag_state.get('job',{}),'indexedDocuments':len(rag_state.get('documents',[]))}
    except Exception:
        pass
    tunnel=_tunnel()
    try:
        import direct_host
        pid=direct_host._managed_pid(direct_host.CADDY_PID,str(direct_host.CADDYFILE.resolve()))
        direct={'state':'healthy' if pid else 'stopped','pid':pid}
    except Exception:direct={'state':'unavailable'}
    try:
        from server_logs import logs
        events=logs(200).get('events',[])
        errors=[e for e in events if e.get('details',{}).get('outcome')=='error' or int(e.get('details',{}).get('http_status') or 0)>=500]
        recent_errors=[{'time':e.get('time'),'event':e.get('event'),'type':e.get('details',{}).get('error_type') or e.get('details',{}).get('http_status')} for e in errors[-10:]]
    except Exception:recent_errors=[]
    database=st
    overall='healthy'
    required=[database.get('state'),storage.get('state')]
    if any(v in ('unavailable','error') for v in required):overall='degraded'
    try:
        import server
        uptime=time.monotonic()-server.SERVER_STARTED
    except Exception:uptime=time.monotonic()-STARTED
    total_mem, available_mem = mem.get('MemTotal'), mem.get('MemAvailable')
    system_cpu = _cpu()
    swap_total, swap_free = mem.get('SwapTotal'), mem.get('SwapFree')
    return {'checkedAt':time.time(),'overall':overall,
            'application':{'state':'healthy','uptimeSeconds':round(uptime)},
            'api':{'state':'healthy','authenticated':True,'responseTimeMs':round((time.monotonic()-started)*1000,1)},
            'resources':{'cpuPercent':system_cpu,'systemCpuPercent':system_cpu,'processCpuPercent':_process_cpu(),
                         'cpuMethod':('/proc/stat counter delta' if system_cpu is not None else (_cpu_unavailable_reason or 'Waiting for a second /proc/stat sample')),
                         'processCpuMethod':'/proc/self/stat CPU-time delta normalized by logical CPU count',
                         'memoryTotalBytes':total_mem,'memoryAvailableBytes':available_mem,
                         'memoryUsedBytes':(total_mem-available_mem) if total_mem is not None and available_mem is not None else None,
                         'memoryAvailablePercent':round(available_mem*100/total_mem,1) if total_mem and available_mem is not None else None,
                         'swapTotalBytes':swap_total,'swapFreeBytes':swap_free,
                         'processRssBytes':_process_rss(),'storage':storage},
            'model':model_info,'openrouter':openrouter,'database':database,
            'index':index_state,'tunnel':tunnel,'directHosting':direct,'recentErrors':recent_errors}


def _process_memory(pid):
    """Return only RSS/virtual bytes for the identified MyForever inference PID."""
    if not isinstance(pid, int) or pid <= 0:
        return {"rssBytes": None, "virtualBytes": None, "process": None}
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        values = {}
        for line in status.splitlines():
            key, sep, rest = line.partition(":")
            if sep and key in ("VmRSS", "VmSize"):
                values[key] = int(rest.split()[0]) * 1024
        try:
            name = Path(f"/proc/{pid}/comm").read_text().strip()[:32]
        except OSError:
            name = None
        return {"rssBytes": values.get("VmRSS"), "virtualBytes": values.get("VmSize"), "process": name}
    except (OSError, ValueError, IndexError):
        return {"rssBytes": None, "virtualBytes": None, "process": None}


def memory_snapshot():
    """Authenticated Agentic Chat memory snapshot. Unsupported data remains null."""
    from datetime import datetime, timezone
    import local_ai
    mem = _meminfo()
    total = mem.get("MemTotal")
    available = mem.get("MemAvailable")
    used = total - available if total is not None and available is not None else None
    backend = _process_rss()
    model_status = local_ai.status()
    inference_pid = local_ai._pid()
    inference = _process_memory(inference_pid)
    active = model_status.get("activeModel") or model_status.get("selectedModel")
    model_name = next((item.get("name") for item in model_status.get("models", []) if item.get("id") == active), None)
    context = dict(getattr(local_ai, "_last_context_usage", {}))
    context_time = context.get("measuredAt")
    context_fresh = bool(context_time and time.time() - context_time < 1800)
    context_used = context.get("usedTokens") if context_fresh else None
    context_max = model_status.get("contextTokens") if model_status.get("loaded") else None
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "system": {"totalBytes": total, "usedBytes": used, "availableBytes": available,
                   "usagePercent": round(used * 100 / total, 1) if used is not None and total else None,
                   "swapTotalBytes": mem.get("SwapTotal"), "swapFreeBytes": mem.get("SwapFree")},
        "process": {"rssBytes": backend, "virtualBytes": _process_memory(os.getpid())["virtualBytes"],
                    "name": "MyForever Python server", "cpuPercent": _process_cpu(),
                    "inferenceRssBytes": inference["rssBytes"],
                    "inferenceVirtualBytes": inference["virtualBytes"], "inferenceProcess": "llama-server (validated active model process)" if inference_pid and inference["rssBytes"] is not None else None},
        "model": {"name": model_name, "id": active, "loaded": bool(model_status.get("loaded")),
                  "memoryBytes": None, "memoryStatus": "not exposed by llama.cpp runtime"},
        "accelerator": {"available": None, "totalBytes": None, "usedBytes": None,
                        "status": "not exposed by the current Termux inference runtime"},
        "context": {"usedTokens": context_used, "maximumTokens": context_max,
                    "inputTokens": context.get("inputTokens") if context_fresh else None,
                    "outputTokens": context.get("outputTokens") if context_fresh else None,
                    "measuredAt": context_time if context_fresh else None},
        "capabilities": {"systemRam": total is not None and available is not None,
                         "serverProcessMemory": backend is not None,
                         "inferenceProcessMemory": inference["rssBytes"] is not None,
                         "modelMemory": False, "acceleratorMemory": False,
                         "contextUsage": context_used is not None},
    }
