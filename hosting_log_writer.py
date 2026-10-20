"""Bounded private log sink that can outlive a restarted MyForever control plane."""
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

_ID = re.compile(r"^[a-f0-9]{32}$")
_MAX = 1_000_000


def _private_values(app_id):
    root = Path.home()/".local"/"share"/"myforever"/"hosting"/"secrets"
    try: config=json.loads((root/(app_id+".json")).read_text(encoding="utf-8"))
    except (OSError,ValueError): return []
    env=config.get("environment",{}) if isinstance(config,dict) else {}
    if not isinstance(env,dict): return []
    values=[]
    for key,value in env.items():
        if isinstance(value,str) and value:values.append(value)
    command=config.get("command",[]) if isinstance(config,dict) else []
    for index,arg in enumerate(command if isinstance(command,list) else []):
        if not isinstance(arg,str):continue
        if "=" in arg and re.search(r"password|secret|token|key|credential",arg.split("=",1)[0],re.I):values.append(arg.split("=",1)[1])
        if index and re.search(r"password|secret|token|key|credential",str(command[index-1]),re.I):values.append(arg)
    return sorted(set(values),key=len,reverse=True)


def main():
    if len(sys.argv)!=2 or not _ID.fullmatch(sys.argv[1]): return 2
    app_id=sys.argv[1]
    base=Path.home()/".local"/"share"/"myforever"/"hosting"
    log=base/"logs"/(app_id+".log")
    log.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    try: os.chmod(log.parent,0o700)
    except OSError: pass
    secrets=_private_values(app_id)
    for raw in sys.stdin.buffer:
        line=raw.decode("utf-8","replace").replace("\x00","")
        for value in secrets: line=line.replace(value,"[REDACTED]")
        data=(time.strftime("%Y-%m-%dT%H:%M:%S%z ")+line.rstrip("\r\n")[:4000]+"\n").encode("utf-8","replace")
        try:
            with log.open("a+b") as stream:
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
                stream.seek(0,os.SEEK_END)
                if stream.tell()+len(data)>_MAX:
                    backup=log.with_suffix(".log.1")
                    try: backup.unlink()
                    except FileNotFoundError: pass
                    stream.close()
                    try: log.replace(backup)
                    except OSError: pass
                    stream=log.open("a+b")
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
                stream.write(data);stream.flush()
                try:os.chmod(log,0o600)
                except OSError:pass
        except OSError:
            # Observability is best-effort; never keep an application alive solely for logging.
            return 1
    return 0


if __name__=="__main__":raise SystemExit(main())
