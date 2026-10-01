"""Host-wide cooperative workload admission and fail-closed campaign observer.

Attaching never patches a worker or kills another agent's work. Supported direct
launches are detected; prevention requires using the shared admission launcher.
"""
from __future__ import annotations
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid

SCHEMA = 'qualification-host-isolation.v1'
DEFAULT_LEASE = (Path(r'C:\ofca-qualification-isolation') if os.name == 'nt'
                 else Path('/mnt/c/ofca-qualification-isolation')) / 'heavy-workload.lock'
CANDIDATE = re.compile(r'^(python[\d.]*w?|pypy[\d.]*|py|pytest.*|py\.test.*|tox.*|nox.*|'
                       r'uv|poetry|hatch|coverage|pip[\d.]*|node.*|npm|pnpm|yarn|cargo|rustc|'
                       r'make|gmake|cmake|ninja|dotnet|msbuild|go|gcc.*|g\+\+.*|clang.*|'
                       r'cc1.*|bash|dash|zsh|fish|sh|cmd|powershell|pwsh|docker|podman)$', re.I)
BENCHMARKS = {'qualify_analytics_baseline.py', 'analytics_qualification_process.py',
              'targeted_visibility_benchmark.py', 'light_first_update_benchmark.py',
              'light_first_update_io_diagnostic.py', 'run_controlled_campaign.py'}
HEAVY_SCRIPT = re.compile(r'^(?:run[_-])?(?:full[_-])?(?:backend[_-])?tests?(?:[_-].*)?\.(?:py|sh|ps1|cmd|bat)$', re.I)

def stamp():
    return dict(utc=datetime.now(timezone.utc).isoformat(), epoch=time.time(), monotonic=time.monotonic())

def basename(value):
    return str(value).replace('\\', '/').rsplit('/', 1)[-1].strip('"\'').lower()

def executable(value):
    return re.sub(r'\.(exe|cmd|bat)$', '', basename(value))

def command_reason(argv, depth=0):
    """Match executable positions, not arbitrary strings mentioning test tools."""
    if not argv or depth > 8:return None
    argv = [str(a) for a in argv]
    name, args = executable(argv[0]), argv[1:]
    if basename(argv[0]) in BENCHMARKS or HEAVY_SCRIPT.fullmatch(basename(argv[0])):
        return 'test_or_benchmark_script'
    if name.startswith(('pytest', 'py.test')) or name in {'tox', 'nox'}:
        if any(a in {'--help','--version','-h'} for a in args):return None
        return 'python_test_suite'
    if name in {'rustc','make','gmake','ninja','msbuild'} or re.fullmatch(r'(?:gcc|g\+\+|clang|cc1).*', name):
        return 'compiler_or_build'
    if name == 'cmake' and '--build' in args:return 'compiler_or_build'
    if name in {'cargo','dotnet','go'} and any(a in {'test','build','bench','publish','install'} for a in args[:3]):
        return 'test_or_build'
    if name in {'npm','pnpm','yarn'}:
        if any(a in {'test','build','benchmark','bench','ci','install','rebuild','compile'} or a.startswith(('test:', 'build:')) for a in args[:4]):
            return 'javascript_test_or_build'
    if name in {'docker','podman'} and any(a in {'build','buildx'} for a in args[:3]):return 'container_build'
    if name in {'uv','poetry','hatch'}:
        for i,a in enumerate(args):
            if a in {'run','exec'}:return command_reason(args[i+1:],depth+1)
        if any(a in {'sync','install','build'} for a in args[:3]):return 'environment_build'
    if name == 'coverage' and args and args[0] == 'run':
        remaining=args[1:]
        if remaining and remaining[0]=='-m':remaining=remaining[1:]
        return command_reason(remaining,depth+1)
    if name.startswith('pip') and any(a in {'install','wheel','download'} for a in args[:2]):return 'environment_build'
    if re.fullmatch(r'(python[\d.]*w?|pypy[\d.]*|py)', name):
        i = 0
        while i < len(args):
            a=args[i]
            if a=='-c':return None
            if a=='-m' and i+1<len(args):
                module=args[i+1]
                if module=='unittest':return 'python_test_suite'
                if module in {'build','PyInstaller'}:return 'environment_build'
                return command_reason([module,*args[i+2:]],depth+1)
            if a in {'-W','-X'}:i+=2;continue
            if a.startswith('-'):i+=1;continue
            return command_reason(args[i:],depth+1)
        return None
    if name.startswith('node'):
        if args and executable(args[0]) in {'npm-cli.js','pnpm.cjs','yarn.js'}:
            manager={'npm-cli.js':'npm','pnpm.cjs':'pnpm','yarn.js':'yarn'}[executable(args[0])]
            return command_reason([manager,*args[1:]],depth+1)
        for a in args[:3]:
            if basename(a) in {'jest.js','vitest.mjs','tsc','webpack.js','vite.js'}:return 'javascript_test_or_build'
        return None
    if name in {'cmd','powershell','pwsh','bash','dash','zsh','fish','sh'}:
        for i,a in enumerate(args):
            if a.lower() in {'-c','-lc','-ic','/c','-command'} and i+1<len(args):
                body=' '.join(args[i+1:])
                try:
                    lex=shlex.shlex(body,posix=(name not in {'cmd','powershell','pwsh'}),punctuation_chars=';&|')
                    lex.whitespace_split=True;lex.commenters='';tokens=list(lex)
                except ValueError:return None
                chunk=[]
                for tok in tokens+[';']:
                    if tok in {';','&&','||','|'}:
                        while chunk and (chunk[0] in {'exec','&','env'} or re.match(r'^\w+=',chunk[0])):chunk.pop(0)
                        if chunk:
                            hit=command_reason(chunk,depth+1)
                            if hit:return hit
                        chunk=[]
                    else:chunk.append(tok)
                return None
            if a.lower() in {'-file','-f'} and i+1<len(args):return command_reason(args[i+1:],depth+1)
        for a in args:
            if not a.startswith('-'):return command_reason([a],depth+1)
    return None

def encode(value):
    return (json.dumps(value,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()

def write_once(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:f.write(encode(value));f.flush();os.fsync(f.fileno())

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:tmp.write_bytes(encode(value));os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)

class LeaseBusy(RuntimeError):pass

@contextmanager
def lease(path,owner):
    """Atomic shared-NTFS directory lease. No stale stealing."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);token=uuid.uuid4().hex
    try:path.mkdir()
    except FileExistsError:raise LeaseBusy(f'Heavy workload admission blocked: {path}') from None
    write_once(path/'owner.json',dict(owner,token=token,at=stamp()))
    try:yield token
    finally:
        try:
            info=json.loads((path/'owner.json').read_text())
            if info['token']!=token:raise RuntimeError('lease_owner_changed')
            if (path/'unsafe-release.json').exists():raise RuntimeError('lease_retained_for_explicit_review')
            (path/'owner.json').unlink();path.rmdir()
        except FileNotFoundError:raise RuntimeError('lease_was_removed_while_owned')

def reserve_boundaries(campaign,reason):
    """The existing controller opens each command log exclusively before Popen."""
    control=campaign/'campaign-control';plan=json.loads((control/'plan.json').read_text());reserved=[]
    for i,_ in enumerate(plan['planned_jobs']):
        path=control/f'{i:02d}-command.log'
        try:write_once(path,dict(schema=SCHEMA,launch_blocked=True,reason=reason,at=stamp()))
        except FileExistsError:continue
        reserved.append(path.name)
    return reserved

def latch(campaign,evidence,issues):
    try:write_once(evidence/'interference.json',dict(schema=SCHEMA,at=stamp(),issues=issues,
        consequence='No clean isolation verdict; block subsequent campaign launches.'))
    except FileExistsError:pass
    reserved=reserve_boundaries(campaign,'host_isolation_guard_failed')
    try:write_once(evidence/'launch-barrier.json',dict(at=stamp(),reserved=reserved))
    except FileExistsError:pass

def owned_descendants(rows,root_pid,root_birth):
    root=rows.get(root_pid)
    if root is None or root['birth']!=root_birth:return set()
    owned={root_pid};changed=True
    while changed:
        changed=False
        for pid,r in rows.items():
            parent=rows.get(r['ppid'])
            if pid not in owned and r['ppid'] in owned and parent and r['birth']>=parent['birth']:
                owned.add(pid);changed=True
    return owned

def evidence_for(row,domain,reason):
    return dict(domain=domain,pid=row['pid'],ppid=row.get('ppid'),birth=row.get('birth'),name=row['name'],reason=reason,
        command_sha256=hashlib.sha256('\0'.join(row.get('argv',[])).encode()).hexdigest())

def authorized_pids(rows, root_pid, root_birth, launcher_identity=None):
    owned=owned_descendants(rows,root_pid,root_birth)
    # Exempt only the bound waiting launcher itself, NOT its other descendants.
    if root_pid in owned and launcher_identity is not None:
        pid,birth=launcher_identity
        if rows[root_pid]['ppid']==pid and rows.get(pid,{}).get('birth')==birth:owned.add(pid)
    return owned


class WindowsScan:
    def __init__(self, launcher_identity=None):
        import psutil
        import ctypes
        from ctypes import wintypes as w
        self.psutil=psutil;self.ct=ctypes;self.launcher_identity=launcher_identity
        class Entry(ctypes.Structure):
            _fields_=[('dwSize',w.DWORD),('cntUsage',w.DWORD),('th32ProcessID',w.DWORD),
                ('th32DefaultHeapID',ctypes.c_size_t),('th32ModuleID',w.DWORD),('cntThreads',w.DWORD),
                ('th32ParentProcessID',w.DWORD),('pcPriClassBase',w.LONG),('dwFlags',w.DWORD),('szExeFile',w.WCHAR*260)]
        self.Entry=Entry;self.dll=ctypes.WinDLL('kernel32',use_last_error=True)
        self.dll.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];self.dll.CreateToolhelp32Snapshot.restype=w.HANDLE
        self.dll.CloseHandle.argtypes=[w.HANDLE];self.dll.CloseHandle.restype=w.BOOL
        for name in ['Process32FirstW','Process32NextW']:
            fn=getattr(self.dll,name);fn.argtypes=[w.HANDLE,ctypes.POINTER(Entry)];fn.restype=w.BOOL
    def table(self):
        ct=self.ct;handle=self.dll.CreateToolhelp32Snapshot(2,0)
        if handle==ct.c_void_p(-1).value:raise ct.WinError(ct.get_last_error())
        rows={};entry=self.Entry();entry.dwSize=ct.sizeof(entry)
        try:
            if not self.dll.Process32FirstW(handle,ct.byref(entry)):raise ct.WinError(ct.get_last_error())
            while True:
                pid=int(entry.th32ProcessID)
                rows[pid]=dict(pid=pid,ppid=int(entry.th32ParentProcessID),name=entry.szExeFile,birth=-1)
                if not self.dll.Process32NextW(handle,ct.byref(entry)):
                    if ct.get_last_error()!=18:raise ct.WinError(ct.get_last_error())
                    break
        finally:self.dll.CloseHandle(handle)
        return rows
    def collect(self,root_pid,root_birth):
        ps=self.psutil;issues=[];started=time.monotonic();rows=self.table()
        candidates={pid for pid,r in rows.items() if CANDIDATE.fullmatch(executable(r['name']))}
        relevant=candidates|{root_pid}
        for pid in list(relevant):
            seen=set()
            while pid in rows and pid not in seen:
                seen.add(pid);relevant.add(pid);pid=rows[pid]['ppid']
        processes={}
        for pid in relevant:
            if pid not in rows or pid==0:continue
            try:
                proc=ps.Process(pid);rows[pid]['birth']=proc.create_time();processes[pid]=proc
            except ps.NoSuchProcess:continue
            except ps.AccessDenied:
                if pid in candidates:issues.append(dict(domain='windows',pid=pid,name=rows[pid]['name'],reason='process_identity_unreadable'))
        own=authorized_pids(rows,root_pid,root_birth,self.launcher_identity)
        for pid in candidates-own:
            r=rows[pid]
            if pid not in processes:continue
            try:
                proc=processes[pid];argv=proc.cmdline()
                if not proc.is_running() or not argv:continue
            except ps.NoSuchProcess:continue
            except ps.AccessDenied:
                issues.append(dict(domain='windows',pid=pid,name=r['name'],reason='candidate_command_unreadable'));continue
            hit=command_reason(argv)
            if hit:issues.append(evidence_for(dict(r,argv=argv),'windows',hit))
        return dict(at=stamp(),seconds=time.monotonic()-started,processes=len(rows),identity_lookups=len(relevant),
                    issues=issues,root_alive=root_pid in own,owned_count=len(own))


class WslObserver:
    """One persistent read-only shell per already-running WSL distribution."""
    def __init__(self,distro,helper,interval,stop_path):
        self.distro=distro;self.latest=None;self.error=None;self.lock=threading.Lock()
        self.detected={};self.stop_path=stop_path;self.interval=interval
        prefix='/mnt/host/c/' if distro=='docker-desktop' else '/mnt/c/'
        path=prefix+str(helper)[3:].replace('\\','/')
        stop=prefix+str(stop_path)[3:].replace('\\','/')
        self.proc=subprocess.Popen(['wsl.exe','--distribution',distro,'--exec','sh',path,str(interval),stop],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        self.thread=threading.Thread(target=self._reader,name='wsl-observer-'+distro,daemon=True);self.thread.start()
    def _reader(self):
        issues=[];count=0;begin=None
        try:
            for raw in self.proc.stdout:
                line=raw.decode('utf-8','strict').rstrip('\r\n')
                if line=='BEGIN':begin=time.monotonic();issues=[];count=0
                elif line.startswith('ROW\t'):
                    _,pid,ppid,birth,name,b64=line.split('\t',5)
                    argv=[x.decode('utf-8','replace') for x in base64.b64decode(b64,validate=True).split(b'\0') if x]
                    row=dict(pid=int(pid),ppid=int(ppid),birth=birth,name=name,argv=argv)
                    hit=command_reason(argv);count+=1
                    if hit:issues.append(evidence_for(row,'wsl:'+self.distro,hit))
                elif line.startswith('ERROR\t'):
                    issues.append(dict(domain='wsl:'+self.distro,reason='candidate_process_unreadable',pid=line.split('\t')[1]))
                elif line=='END' and begin is not None:
                    with self.lock:
                        for issue in issues:self.detected[json.dumps(issue,sort_keys=True)]=issue
                        self.latest=dict(received=time.monotonic(),seconds=time.monotonic()-begin,issues=list(self.detected.values()),candidates=count)
                else:raise ValueError('unexpected_wsl_record')
        except Exception as e:
            with self.lock:self.error=type(e).__name__
    def snapshot(self):
        with self.lock:return self.latest,self.error
    def close(self):
        self.stop_path.parent.mkdir(parents=True,exist_ok=True);self.stop_path.touch(exist_ok=False)
        try:self.proc.wait(timeout=self.interval+10)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)
        self.thread.join(timeout=5)
        if self.proc.stdout:self.proc.stdout.close()
        if self.proc.stderr:self.proc.stderr.close()
        return not self.thread.is_alive()


def running_distros():
    result=subprocess.run(['wsl.exe','--list','--running','--quiet'],capture_output=True,timeout=5,check=True)
    raw=result.stdout;text=raw.decode('utf-16-le') if b'\0' in raw else raw.decode('utf-8-sig')
    return sorted(s.strip().lstrip('\ufeff') for s in text.splitlines() if s.strip())


def run_guard(args):
    if os.name!='nt':raise RuntimeError('attach supervisor must run on Windows')
    import psutil
    campaign=args.campaign.resolve();evidence=campaign/'campaign-control'/'host-isolation';evidence.mkdir(exist_ok=False)
    control=psutil.Process(args.controller_pid);root_birth=control.create_time();controller_command=control.cmdline()
    plan_path=campaign/'campaign-control'/'plan.json';plan=json.loads(plan_path.read_text())
    script=next((Path(a) for a in controller_command if basename(a)=='run_controlled_campaign.py'),None)
    if script is None or hashlib.sha256(script.read_bytes()).hexdigest()!=plan['controller_sha256']:
        raise RuntimeError('controller_hash_mismatch')
    if "with log_path.open('xb') as log:" not in script.read_text():raise RuntimeError('controller_launch_barrier_not_supported')
    observers=[];samples=0;bad=False;started=stamp();max_scan=0;scan_total=0;gaps=[]
    last_scan=None;stop_reason=None;start_cpu=time.process_time();ready=False;last_publish=0;joined=True
    owner=dict(kind='qualification',controller_pid=args.controller_pid,controller_birth=root_birth,campaign=str(campaign),
        campaign_id=plan['campaign_id'],campaign_plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        guard_pid=os.getpid(),script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    with lease(args.lease,owner):
        write_once(evidence/'attachment.json',dict(schema=SCHEMA,at=started,owner=owner,interval_seconds=args.interval,
            maximum_heartbeat_gap_seconds=args.max_gap,scope='Windows and process namespaces visible in all listed running WSL distributions',
            prior_campaign_time='NOT_OBSERVED',no_process_termination=True,no_source_or_worker_changes=True,
            admission='Cooperating launchers must use run; arbitrary direct launches are detected, not OS-prohibited.',original_results_preserved=True))
        try:
            distros=running_distros();write_once(evidence/'wsl-coverage.json',dict(at=stamp(),distros=distros))
            for name in distros:
                stop=evidence/'observer-stop'/hashlib.sha256(name.encode()).hexdigest()
                observers.append(WslObserver(name,args.wsl_helper,args.interval,stop))
            launcher=control.parent()
            scan=WindowsScan((launcher.pid,launcher.create_time()) if launcher else None);next_topology=0
            while True:
                now=time.monotonic();issues=[]
                if last_scan is not None:
                    gaps.append(now-last_scan)
                    if now-last_scan>args.max_gap:issues.append(dict(reason='windows_observer_gap',seconds=now-last_scan))
                last_scan=now
                w=scan.collect(args.controller_pid,root_birth);samples+=1;max_scan=max(max_scan,w['seconds']);scan_total+=w['seconds']
                issues.extend(w['issues']);states={}
                for ob in observers:
                    latest,error=ob.snapshot();states[ob.distro]=latest
                    if error:issues.append(dict(domain='wsl:'+ob.distro,reason='observer_error',error=error))
                    elif ob.proc.poll() is not None:issues.append(dict(domain='wsl:'+ob.distro,reason='observer_exited'))
                    elif latest is None:
                        if now-started['monotonic']>args.max_gap:issues.append(dict(domain='wsl:'+ob.distro,reason='observer_no_heartbeat'))
                    elif now-latest['received']>args.max_gap:issues.append(dict(domain='wsl:'+ob.distro,reason='observer_stale'))
                    else:issues.extend(latest['issues'])
                if now>=next_topology:
                    actual=running_distros();next_topology=now+30
                    if actual!=distros:issues.append(dict(reason='wsl_topology_changed',expected=distros,observed=actual))
                if issues:bad=True;latch(campaign,evidence,issues)
                if not ready and all(s is not None for s in states.values()):
                    ready=True;write_once(evidence/'armed.json',dict(at=stamp(),state='FAILED' if bad else 'ARMED',windows=w,wsl=states,coverage_begins_now=True))
                if now-last_publish>=15 or not w['root_alive'] or issues:
                    atomic(evidence/'status.json',dict(at=stamp(),state='FAILED' if bad else 'ARMED' if ready else 'STARTING',
                        controller_alive=w['root_alive'],samples=samples,windows_scan_seconds=w['seconds'],
                        guard_cpu_seconds=time.process_time()-start_cpu,windows_issues=w['issues'],wsl=states));last_publish=now
                if not w['root_alive']:stop_reason='controller_exited';break
                time.sleep(max(0.05,args.interval-(time.monotonic()-now)))
        except BaseException as e:
            bad=True;stop_reason='guard_error:'+type(e).__name__;latch(campaign,evidence,[dict(reason=stop_reason)])
        finally:
            for ob in observers:joined=ob.close() and joined
            final=dict(schema=SCHEMA,at=stamp(),status='FAIL' if bad else 'CLEAN_SINCE_ATTACHMENT' if ready else 'BLOCKED',
                complete=True,source_modified=False,stop_reason=stop_reason,started=started,samples=samples,
                windows_scan_seconds_total=scan_total,windows_scan_seconds_max=max_scan,maximum_observation_gap_seconds=max(gaps,default=0),
                guard_cpu_seconds=time.process_time()-start_cpu,observer_children_joined=joined,full_campaign_isolation_proven=False,
                late_attachment_note='Earlier campaign results remain original evidence, not retrospectively guarded.')
            if not joined:final.update(status='FAIL',complete=False)
            write_once(evidence/'result.json',final);print(json.dumps(final,sort_keys=True),flush=True)
    return 2 if bad else 0


def guarded_run(args):
    command=list(args.command)
    if command and command[0]=='--':command.pop(0)
    if not command:raise ValueError('missing workload command')
    try:
        with lease(args.lease,dict(kind='heavy-workload',pid=os.getpid(),label=args.label)):
            import psutil
            child=subprocess.Popen(command,start_new_session=os.name!='nt');known={}
            try:
                while child.poll() is None:
                    try:
                        for proc in psutil.Process(child.pid).children(recursive=True):known[proc.pid]=proc.create_time()
                    except psutil.NoSuchProcess:pass
                    except psutil.AccessDenied:
                        if not (args.lease/'unsafe-release.json').exists():write_once(args.lease/'unsafe-release.json',dict(at=stamp(),reason='child_ownership_unreadable'))
                    try:child.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:pass
                remaining=[]
                for pid,birth in known.items():
                    try:
                        proc=psutil.Process(pid)
                        if proc.create_time()==birth and proc.status()!=psutil.STATUS_ZOMBIE:remaining.append(pid)
                    except psutil.NoSuchProcess:pass
                if os.name!='nt':
                    for proc in psutil.process_iter(['pid','status']):
                        try:
                            if os.getpgid(proc.pid)==child.pid and proc.status()!=psutil.STATUS_ZOMBIE:remaining.append(proc.pid)
                        except (ProcessLookupError,PermissionError):pass
                if remaining and not (args.lease/'unsafe-release.json').exists():
                    write_once(args.lease/'unsafe-release.json',dict(at=stamp(),reason='workload_left_children',pids=sorted(set(remaining))))
                return child.returncode
            except BaseException:
                if not (args.lease/'unsafe-release.json').exists():write_once(args.lease/'unsafe-release.json',dict(at=stamp(),reason='workload_owner_interrupted',child_pid=child.pid))
                raise
    except LeaseBusy as e:print(str(e),file=sys.stderr);return 75


def assess_campaign(campaign):
    evidence=campaign/'campaign-control'/'host-isolation'
    if (evidence/'interference.json').exists():return dict(status='FAIL',reason='host_interference_or_coverage_failure_latched')
    final=evidence/'result.json'
    if not final.exists():return dict(status='BLOCKED',reason='guard_has_no_final_receipt')
    data=json.loads(final.read_text())
    if not data.get('complete') or not data.get('observer_children_joined'):return dict(status='BLOCKED',reason='guard_shutdown_incomplete')
    return dict(status=data['status'],full_campaign_isolation_proven=False,reason='guard_was_attached_after_campaign_start',coverage_started=data['started'])


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    a=sub.add_parser('attach');a.add_argument('--campaign',type=Path,required=True);a.add_argument('--controller-pid',type=int,required=True)
    a.add_argument('--lease',type=Path,default=DEFAULT_LEASE);a.add_argument('--wsl-helper',type=Path,required=True)
    a.add_argument('--interval',type=float,default=5);a.add_argument('--max-gap',type=float,default=20)
    r=sub.add_parser('run');r.add_argument('--lease',type=Path,default=DEFAULT_LEASE);r.add_argument('--label',default='test-or-build');r.add_argument('command',nargs=argparse.REMAINDER)
    c=sub.add_parser('check');c.add_argument('--lease',type=Path,default=DEFAULT_LEASE)
    v=sub.add_parser('assess');v.add_argument('--campaign',type=Path,required=True)
    args=p.parse_args()
    if args.mode=='attach':
        if args.interval<2 or args.max_gap<args.interval*2:p.error('sampling interval too aggressive or gap limit too small')
        return run_guard(args)
    if args.mode=='run':return guarded_run(args)
    if args.mode=='assess':
        result=assess_campaign(args.campaign);print(json.dumps(result,sort_keys=True));return 0 if result['status']=='CLEAN_SINCE_ATTACHMENT' else 2
    if args.lease.exists():print('BUSY: qualification or heavy workload owns '+str(args.lease));return 75
    print('FREE (informational only; run acquires atomically)');return 0

if __name__=='__main__':raise SystemExit(main())
