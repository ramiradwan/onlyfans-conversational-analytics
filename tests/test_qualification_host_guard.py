import argparse
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tools import qualification_host_guard as g

class ClassificationTests(unittest.TestCase):
    def test_heavy_command_matrix(self):
        commands=[['python','-m','pytest','tests/'],[r'C:\venv\Scripts\python.exe','-u','-m','pytest','-q'],
            ['py','-3.13','-m','pytest'],['/tmp/venv/bin/python','/tmp/venv/bin/pytest','-q'],
            ['pytest.exe','tests/'],['tox','-e','py'],['nox','-s','tests'],['python','-m','unittest','discover'],
            ['coverage','run','-m','pytest'],['uv','run','pytest','tests/'],['poetry','run','pytest','-q'],
            ['npm','run','test:backend'],['pnpm','run','build'],['yarn','test'],
            ['node',r'C:\npm\npm-cli.js','run','test:backend'],['cargo','test'],['cargo','build','--release'],
            ['dotnet','test'],['go','test','./...'],['make','all'],['cmake','--build','.'],
            ['bash','-lc','cd /tmp/repo && pytest tests/'],['bash','-c','cd /tmp/repo;pytest tests/'],
            ['powershell.exe','-File',r'C:\repo\run_backend_tests.ps1'],
            ['powershell.exe','-Command','& python -m pytest tests/'],
            ['python','tools/qualify_analytics_baseline.py','--closure'],['python','-u','run_full_tests.py'],
            ['rustc','a.rs'],['docker','build','.'],['python','-m','pip','install','-r','requirements.txt']]
        for cmd in commands:
            with self.subTest(cmd=cmd):self.assertIsNotNone(g.command_reason(cmd))
    def test_benign_commands_do_not_match_text_literals(self):
        commands=[['python','-c','print("pytest")'],['git','grep','pytest'],['bash','-lc',"printf '%s' pytest"],
            ['pytest','--version'],['node','editor.js','pytest'],['powershell','-Command',"Write-Output 'pytest'"],
            ['python','qualification_host_guard.py','attach'],['sh','qualification_wsl_process_observer.sh','5'],['cargo','--version']]
        for cmd in commands:
            with self.subTest(cmd=cmd):self.assertIsNone(g.command_reason(cmd))
    def test_unknowns_are_not_evaluated(self):
        self.assertIsNone(g.command_reason(['python','-c','__import__("os").system("pytest")']))
    def test_exemption_is_identity_not_name(self):
        rows={1:dict(pid=1,ppid=0,birth=10),2:dict(pid=2,ppid=1,birth=11),3:dict(pid=3,ppid=2,birth=12),4:dict(pid=4,ppid=0,birth=11)}
        self.assertEqual(g.owned_descendants(rows,1,10),{1,2,3});self.assertEqual(g.owned_descendants(rows,1,9),set())
    def test_reused_parent_pid_does_not_exempt_older_child(self):
        rows={1:dict(pid=1,ppid=0,birth=10),2:dict(pid=2,ppid=1,birth=9)}
        self.assertEqual(g.owned_descendants(rows,1,10),{1})
    def test_evidence_redacts_arguments(self):
        row=dict(pid=1,ppid=0,birth=10,name='python',argv=['python','-m','pytest','--password=secret'])
        out=g.evidence_for(row,'windows','test');self.assertNotIn('secret',json.dumps(out));self.assertEqual(len(out['command_sha256']),64)

class LauncherTests(unittest.TestCase):
    def test_waiting_launcher_does_not_exempt_a_sibling_workload(self):
        rows={1:dict(pid=1,ppid=0,birth=1),2:dict(pid=2,ppid=1,birth=2),3:dict(pid=3,ppid=2,birth=3),4:dict(pid=4,ppid=1,birth=3)}
        self.assertEqual(g.authorized_pids(rows,2,2,(1,1)),{1,2,3})
    def test_changed_launcher_identity_is_not_exempt(self):
        rows={1:dict(pid=1,ppid=0,birth=1),2:dict(pid=2,ppid=1,birth=2)}
        self.assertEqual(g.authorized_pids(rows,2,2,(1,0)),{2})

class AdmissionTests(unittest.TestCase):
    def test_second_owner_blocked_and_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lease'
            with g.lease(path,dict(label='qualification')):
                with self.assertRaises(g.LeaseBusy):
                    with g.lease(path,dict(label='tests')):pass
            self.assertFalse(path.exists())
    def test_foreign_or_stale_lease_is_never_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lease';path.mkdir()
            with self.assertRaises(g.LeaseBusy):
                with g.lease(path,{}):pass
            self.assertTrue(path.exists())
    def test_unsafe_shutdown_leaves_owner_and_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lease'
            with self.assertRaisesRegex(RuntimeError,'retained'):
                with g.lease(path,{}):g.write_once(path/'unsafe-release.json',{'reason':'uncertain'})
            self.assertTrue((path/'owner.json').exists())
    def test_refusal_happens_before_popen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lease';args=argparse.Namespace(command=['python','-c','raise RuntimeError()'],label='tests',lease=path)
            with g.lease(path,{}),patch.object(subprocess,'Popen') as popen,redirect_stderr(io.StringIO()):
                self.assertEqual(g.guarded_run(args),75);popen.assert_not_called()
    def test_guard_slot_cannot_race_second_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'lease'
            with g.lease(path,{}):
                proc=subprocess.run([sys.executable,str(Path(g.__file__)),'run','--lease',str(path),'--',sys.executable,'-c','print("MUST_NOT_RUN")'],capture_output=True,text=True)
                self.assertEqual(proc.returncode,75);self.assertNotIn('MUST_NOT_RUN',proc.stdout)

class BoundaryTests(unittest.TestCase):
    def prepare(self,root):
        c=root/'campaign-control';c.mkdir();(c/'plan.json').write_text(json.dumps({'planned_jobs':['visibility/0','visibility/1','visibility/2','matrix']}))
        (c/'00-command.log').write_text('completed');(c/'01-command.log').write_text('active');return c
    def test_latched_interference_blocks_next_exclusive_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);c=self.prepare(root);e=c/'host-isolation';e.mkdir();g.latch(root,e,[{'reason':'pytest'}]);before=(e/'interference.json').read_bytes()
            g.latch(root,e,[]);self.assertEqual(before,(e/'interference.json').read_bytes());self.assertEqual((c/'01-command.log').read_text(),'active')
            with self.assertRaises(FileExistsError):(c/'02-command.log').open('xb')
    def test_existing_scope_barrier_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);c=self.prepare(root);(c/'03-command.log').write_text('scope');g.reserve_boundaries(root,'interference')
            self.assertEqual((c/'03-command.log').read_text(),'scope')
    def test_atomic_status_does_not_corrupt_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'status.json';g.atomic(p,{'n':1});g.atomic(p,{'n':2});self.assertEqual(json.loads(p.read_text()),{'n':2})
    def test_once_records_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'record.json';g.write_once(p,{'first':True})
            with self.assertRaises(FileExistsError):g.write_once(p,{})

class VerdictTests(unittest.TestCase):
    def test_missing_final_cannot_be_a_clean_result(self):
        with tempfile.TemporaryDirectory() as tmp:self.assertEqual(g.assess_campaign(Path(tmp))['status'],'BLOCKED')
    def test_a_clean_later_sample_cannot_erase_interference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);e=root/'campaign-control'/'host-isolation';e.mkdir(parents=True);(e/'interference.json').write_text('{}')
            (e/'result.json').write_text(json.dumps({'status':'CLEAN_SINCE_ATTACHMENT','complete':True,'observer_children_joined':True,'started':{}}))
            self.assertEqual(g.assess_campaign(root)['status'],'FAIL')
    def test_clean_attachment_never_claims_retroactive_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);e=root/'campaign-control'/'host-isolation';e.mkdir(parents=True)
            (e/'result.json').write_text(json.dumps({'status':'CLEAN_SINCE_ATTACHMENT','complete':True,'observer_children_joined':True,'started':{}}))
            self.assertFalse(g.assess_campaign(root)['full_campaign_isolation_proven'])
    def test_failed_observer_join_is_not_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);e=root/'campaign-control'/'host-isolation';e.mkdir(parents=True)
            (e/'result.json').write_text(json.dumps({'status':'CLEAN_SINCE_ATTACHMENT','complete':True,'observer_children_joined':False,'started':{}}))
            self.assertEqual(g.assess_campaign(root)['status'],'BLOCKED')

if __name__=='__main__':unittest.main()
