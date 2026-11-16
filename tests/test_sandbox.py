import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from xiaolvji.contracts import Fault, digest
from xiaolvji.sandbox import LinuxSandbox, MAX_HOME_BYTES, PRIVATE_HOME, readable_host_sources, seccomp_filter


class SandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as parent:
            cls.capability = LinuxSandbox(Path(parent) / "state").probe()
        if not cls.capability["supported"]:
            raise unittest.SkipTest("Real Linux sandbox unavailable: " + str(cls.capability))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "controller"
        self.state.mkdir()
        self.secret = self.state / "reviewer.key"
        self.secret.write_text("synthetic-private-reviewer-marker")
        self.sandbox = LinuxSandbox(self.state)

    def tearDown(self):
        self.temporary.cleanup()

    def execute(self, code, *, env=None, **policy):
        with self.sandbox.prepare([sys.executable, "-I", "-S", "-c", code], self.work,
                                  env or {}, **policy) as launch:
            result = subprocess.run(launch.argv, env=launch.env, pass_fds=launch.pass_fds,
                                    close_fds=True, capture_output=True, text=True, timeout=8)
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(result.stdout), launch.evidence

    def test_real_probe_observes_kernel_boundary(self):
        observed = self.capability["observed"]
        self.assertTrue(self.capability["strict_supported"])
        self.assertFalse(self.capability["model_network_supported"])
        self.assertFalse(self.capability["resource_limits_enforced"])
        self.assertTrue(observed["ptrace_denied"])
        self.assertTrue(observed["root_readonly"])
        self.assertFalse(observed["hidden"])

    def test_readable_host_sources_cover_exact_implicit_launch_mounts(self):
        sources = readable_host_sources()
        self.assertIsInstance(sources, tuple)
        self.assertEqual(len(sources), len(set(sources)))
        self.assertTrue(all(path.is_absolute() and path.resolve() == path for path in sources))
        # With no extra RO inputs or Git metadata, every --ro-bind here is an
        # implicit runtime grant; workspace and scratch are separate RW mounts.
        with self.sandbox.prepare([sys.executable], self.work, {}) as launch:
            arguments = launch.argv[:launch.argv.index("--")]
            mounted = {Path(arguments[index + 1]) for index, option in enumerate(arguments)
                       if option == "--ro-bind"}
        self.assertEqual(mounted, set(sources))

    def test_private_file_under_python_prefix_is_a_readable_host_source(self):
        # Runtime mounts under host /tmp are hidden by the later private /tmp
        # mount. Place this synthetic prefix beside the checkout, outside that
        # shadowed destination, without modifying the installed Python runtime.
        temporary = tempfile.TemporaryDirectory(prefix="xlj-runtime-fixture-", dir=Path.cwd().parent)
        self.addCleanup(temporary.cleanup)
        runtime = Path(temporary.name) / "synthetic-runtime"
        runtime.mkdir()
        marker = runtime / "private-marker"
        marker.write_text("synthetic-runtime-private-data")
        marker.chmod(0o600)
        alias = Path(temporary.name) / "runtime-alias"
        alias.symlink_to(runtime, target_is_directory=True)
        with patch("xiaolvji.sandbox.sys.prefix", str(alias)):
            sources = readable_host_sources()
            self.assertIn(runtime, sources)
            self.assertNotIn(alias, sources)
            self.assertTrue(any(marker.is_relative_to(source) for source in sources))
            code = """
import json,os,pathlib
path=pathlib.Path(MARKER)
print(json.dumps({'content':path.read_text(),'readonly':bool(os.statvfs(path).f_flag&os.ST_RDONLY)}))
""".replace("MARKER", repr(str(marker)))
            observed, _ = self.execute(code, write_scope=[])
        # A read-only mount does not make a mapped-UID 0600 file confidential;
        # credential admission must reject these sources before reading keys.
        self.assertEqual("synthetic-runtime-private-data", observed["content"])
        self.assertTrue(observed["readonly"])

    def test_private_home_temp_and_unmounted_controller(self):
        code = """
import json,os,pathlib
pathlib.Path('result').write_text('workspace')
pathlib.Path(os.environ['HOME'],'private-marker').write_text('private-home')
pathlib.Path('/tmp/private-marker').write_text('private-temp')
print(json.dumps({'home':os.environ['HOME'], 'secret_exists':pathlib.Path(SECRET).exists(),
 'root_ro':bool(os.statvfs('/').f_flag & os.ST_RDONLY),
 'home_contents':[p.name for p in pathlib.Path(os.environ['HOME']).iterdir()]}))
""".replace("SECRET", repr(str(self.secret)))
        observed, evidence = self.execute(code)
        self.assertEqual(PRIVATE_HOME, observed["home"])
        self.assertEqual(["private-marker"], observed["home_contents"])
        self.assertFalse(observed["secret_exists"])
        self.assertTrue(observed["root_ro"])
        self.assertEqual("workspace", (self.work / "result").read_text())
        self.assertFalse((self.root / "private-marker").exists())
        self.assertEqual(digest(evidence["policy"]), evidence["policy_digest"])

    def test_directory_scope_blocks_sibling_git_symlink_and_hardlink_writes(self):
        (self.work / "allowed").mkdir()
        (self.work / "sibling.txt").write_text("original")
        (self.work / ".git").mkdir()
        (self.work / ".git/config").write_text("untouched")
        code = """
import errno,json,os,pathlib
pathlib.Path('allowed/new.txt').write_text('allowed')
denied={}
for name in ['sibling.txt','.git/config']:
 try: pathlib.Path(name).write_text('forbidden'); denied[name]=False
 except OSError: denied[name]=True
pathlib.Path('allowed/link').symlink_to('../sibling.txt')
try: pathlib.Path('allowed/link').write_text('escape'); denied['symlink']=False
except OSError: denied['symlink']=True
try: os.link('sibling.txt','allowed/hardlink'); denied['hardlink']=False
except OSError as exc: denied['hardlink']=exc.errno in (errno.EXDEV,errno.EPERM,errno.EROFS)
pathlib.Path('allowed/outside').symlink_to(SECRET)
try: pathlib.Path('allowed/outside').read_text(); denied['outside']=False
except OSError: denied['outside']=True
print(json.dumps(denied))
""".replace("SECRET", repr(str(self.secret)))
        observed, _ = self.execute(code, write_scope=[{"path": "allowed", "kind": "directory"}])
        self.assertTrue(all(observed.values()), observed)
        self.assertEqual("original", (self.work / "sibling.txt").read_text())
        self.assertEqual("untouched", (self.work / ".git/config").read_text())
        self.assertEqual("allowed", (self.work / "allowed/new.txt").read_text())
        self.assertEqual("synthetic-private-reviewer-marker", self.secret.read_text())

    def test_empty_scope_is_readonly_source_and_private_tmp_is_writable(self):
        (self.work / "source.py").write_text("original")
        code = """
import json,os,pathlib
denied=[]
for path in ('source.py','new.py'):
 try: pathlib.Path(path).write_text('bad'); denied.append(False)
 except OSError: denied.append(True)
pathlib.Path('/tmp/cache').write_text('ok')
print(json.dumps({'denied':denied,'source_ro':bool(os.statvfs('.').f_flag & os.ST_RDONLY),
 'bytecode':os.environ['PYTHONDONTWRITEBYTECODE']}))
"""
        observed, _ = self.execute(code, write_scope=[])
        self.assertEqual([True, True], observed["denied"])
        self.assertTrue(observed["source_ro"])
        self.assertEqual("1", observed["bytecode"])
        self.assertEqual("original", (self.work / "source.py").read_text())
        self.assertFalse((self.work / "new.py").exists())

    def test_exact_file_scope_does_not_grant_parent_and_cannot_atomic_replace(self):
        (self.work / "exact.txt").write_text("original")
        code = """
import json,os,pathlib
pathlib.Path('exact.txt').write_text('direct-write')
denied={}
try: pathlib.Path('sibling.txt').write_text('bad'); denied['create_sibling']=False
except OSError: denied['create_sibling']=True
pathlib.Path('/tmp/replacement').write_text('replacement')
try: os.replace('/tmp/replacement','exact.txt'); denied['atomic_replace']=False
except OSError: denied['atomic_replace']=True
print(json.dumps(denied))
"""
        observed, _ = self.execute(code, write_scope=[{"path": "exact.txt", "kind": "file"}])
        self.assertTrue(all(observed.values()))
        self.assertEqual("direct-write", (self.work / "exact.txt").read_text())

    def test_future_scopes_require_controller_precreation(self):
        for kind in ("file", "directory"):
            with self.assertRaises(Fault):
                self.sandbox.prepare([sys.executable, "-c", "pass"], self.work, {},
                                     write_scope=[{"path": "future", "kind": kind}])
        self.assertFalse((self.work / "future").exists())
        (self.work / "future").mkdir()
        observed, _ = self.execute("import pathlib,json; pathlib.Path('future/new.py').write_text('ok'); print(json.dumps(True))",
                                  write_scope=[{"path": "future", "kind": "directory"}])
        self.assertTrue(observed)

    def test_additional_grants_remain_narrow_and_readonly_as_declared(self):
        config = self.root / "input.json"
        config.write_text('{"input":true}')
        output = self.root / "output"
        output.mkdir()
        code = """
import json,pathlib
value=json.loads(pathlib.Path(CONFIG).read_text())
pathlib.Path(OUTPUT,'report').write_text('ok')
try: pathlib.Path(CONFIG).write_text('bad'); value['input_readonly']=False
except OSError: value['input_readonly']=True
value['controller_hidden']=not pathlib.Path(SECRET).exists()
print(json.dumps(value))
""".replace("CONFIG", repr(str(config))).replace("OUTPUT", repr(str(output))).replace("SECRET", repr(str(self.secret)))
        observed, _ = self.execute(code, write_scope=[], readonly_paths=[config], writable_paths=[output])
        self.assertEqual({"input": True, "input_readonly": True, "controller_hidden": True}, observed)
        self.assertEqual("ok", (output / "report").read_text())

    def test_preexisting_hardlinks_are_rejected_even_for_readonly_workspace(self):
        os.link(self.secret, self.work / "hardlink")
        with self.assertRaises(Fault) as caught:
            self.sandbox.prepare([sys.executable], self.work, {}, write_scope=[])
        self.assertIn("hardlink", str(caught.exception))

    def test_preexisting_escaping_symlink_and_symlink_scope_are_rejected(self):
        (self.work / "escape").symlink_to(self.secret)
        with self.assertRaises(Fault):
            self.sandbox.prepare([sys.executable], self.work, {})
        (self.work / "escape").unlink()
        (self.work / "real").mkdir()
        (self.work / "alias").symlink_to("real", target_is_directory=True)
        with self.assertRaises(Fault):
            self.sandbox.prepare([sys.executable], self.work, {}, write_scope=[{"path": "alias", "kind": "directory"}])

    def test_preexisting_ipc_endpoints_are_rejected(self):
        os.mkfifo(self.work / "pipe")
        with self.assertRaises(Fault):
            self.sandbox.prepare([sys.executable], self.work, {})
        (self.work / "pipe").unlink()
        # socketpair also works in CI environments that deny socket() itself.
        server, peer = socket.socketpair()
        with server, peer:
            server.bind(str(self.work / "service.sock"))
            with self.assertRaises(Fault):
                self.sandbox.prepare([sys.executable], self.work, {}, write_scope=[])

    def test_full_workspace_grant_still_keeps_git_administration_readonly(self):
        (self.work / ".git").mkdir()
        (self.work / ".git/config").write_text("original")
        code = """
import json,pathlib
pathlib.Path('new.txt').write_text('allowed')
try: pathlib.Path('.git/config').write_text('bad'); denied=False
except OSError: denied=True
print(json.dumps(denied))
"""
        observed, _ = self.execute(code)
        self.assertTrue(observed)
        self.assertEqual("original", (self.work / ".git/config").read_text())

    def test_controller_ancestor_system_and_git_grants_are_rejected(self):
        policies = [
            {"readonly_paths": [self.state]}, {"writable_paths": [self.state]},
            {"readonly_paths": [self.root]}, {"writable_paths": ["/tmp"]},
            {"readonly_paths": ["/usr"]}, {"workspace": self.root},
        ]
        (self.work / ".git").mkdir()
        policies.append({"write_scope": [{"path": ".git", "kind": "directory"}]})
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(Fault):
                self.sandbox.prepare([sys.executable], self.work, {}, **policy)

    def test_socket_seccomp_denies_all_non_unix_families(self):
        code = """
import errno,json,socket
denied={}
for name in ('AF_INET','AF_INET6','AF_NETLINK'):
 try: sock=socket.socket(getattr(socket,name)); sock.close(); denied[name]=False
 except OSError as exc: denied[name]=exc.errno==errno.EPERM
a,b=socket.socketpair(); a.send(b'x'); denied['local_pair_works']=b.recv(1)==b'x'; a.close(); b.close()
print(json.dumps(denied))
"""
        observed, _ = self.execute(code)
        self.assertTrue(all(observed.values()), observed)

    def test_syscall_filter_denies_ptrace_namespaces_and_new_mounts(self):
        code = """
import ctypes,errno,json
libc=ctypes.CDLL(None,use_errno=True)
calls={'ptrace':lambda:libc.ptrace(0,0,0,0), 'unshare':lambda:libc.unshare(0x10000000),
 'setns':lambda:libc.setns(-1,0),'mount':lambda:libc.mount(None,None,None,0,None)}
denied={}
for name,call in calls.items():
 ctypes.set_errno(0); result=call(); denied[name]=result==-1 and ctypes.get_errno()==errno.EPERM
print(json.dumps(denied))
"""
        observed, _ = self.execute(code)
        self.assertTrue(all(observed.values()), observed)

    def test_outside_owned_process_cannot_be_signaled_or_read_via_proc(self):
        outside = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
        try:
            code = """
import errno,json,os,pathlib,signal
try: os.kill(PID,signal.SIGTERM); denied=False
except OSError as exc: denied=exc.errno in (errno.ESRCH,errno.EPERM)
print(json.dumps({'signal_denied':denied,'outside_proc_hidden':not pathlib.Path('/proc/PID/environ').exists()}))
""".replace("PID", str(outside.pid))
            observed, _ = self.execute(code)
            self.assertEqual({"signal_denied": True, "outside_proc_hidden": True}, observed)
            self.assertIsNone(outside.poll())
        finally:
            outside.terminate()
            outside.wait(timeout=3)

    def test_proc_root_and_fd_paths_do_not_reveal_host_authority(self):
        code = """
import json,os,pathlib
paths=[SECRET,'/proc/1/root'+SECRET,'/proc/self/root'+SECRET]
hidden=all(not pathlib.Path(p).exists() for p in paths)
fds={}
for name in os.listdir('/proc/self/fd'):
 try: fds[name]=os.readlink('/proc/self/fd/'+name)
 except FileNotFoundError: pass
print(json.dumps({'hidden':hidden,'fds':fds}))
""".replace("SECRET", repr(str(self.secret)))
        observed, _ = self.execute(code)
        self.assertTrue(observed["hidden"])
        self.assertTrue(set(observed["fds"]) <= {"0", "1", "2"}, observed)

    def test_environment_does_not_pass_host_credentials_or_import_injection(self):
        observed, _ = self.execute("import json,os; print(json.dumps(dict(os.environ)))", env={
            "OPENAI_API_KEY": "synthetic-provider-secret", "HOME": str(self.state),
            "CODEX_HOME": str(self.state), "LD_PRELOAD": "/does/not/exist",
            "PYTHONPATH": str(self.state), "PYTHONDONTWRITEBYTECODE": "0",
            "PARALLEL_DEV_ROLE": "reviewer", "PARALLEL_DEV_ATTEMPT_ID": "attempt-1",
            "PARALLEL_DEV_REVIEWER_TOKEN": "synthetic-authority", "LANG": "C"})
        self.assertFalse({"OPENAI_API_KEY", "LD_PRELOAD", "PYTHONPATH", "PARALLEL_DEV_REVIEWER_TOKEN"} & observed.keys())
        self.assertEqual(PRIVATE_HOME, observed["HOME"])
        self.assertEqual("worker", observed["PARALLEL_DEV_ROLE"])
        self.assertEqual("attempt-1", observed["PARALLEL_DEV_ATTEMPT_ID"])
        self.assertEqual("1", observed["PYTHONDONTWRITEBYTECODE"])

    def test_forks_and_threads_work_but_detached_descendants_die_on_exit(self):
        code = """
import json,os,pathlib,threading,time
events=[]; t=threading.Thread(target=lambda:events.append('thread')); t.start(); t.join()
pid=os.fork()
if pid==0:
 os.setsid()
 if os.fork(): os._exit(0)
 pathlib.Path('ready').write_text('yes')
 time.sleep(0.4)
 pathlib.Path('escaped').write_text('bad')
 os._exit(0)
os.waitpid(pid,0)
deadline=time.monotonic()+2
while not pathlib.Path('ready').exists() and time.monotonic()<deadline: time.sleep(0.005)
print(json.dumps(events))
"""
        observed, _ = self.execute(code)
        self.assertEqual(["thread"], observed)
        self.assertTrue((self.work / "ready").exists())
        time.sleep(0.5)
        self.assertFalse((self.work / "escaped").exists())

    def test_provider_channel_pins_only_the_requested_socketpair(self):
        server, channel = socket.socketpair()
        with server, channel:
            server.settimeout(2)
            server.sendall(b"provider")
            original = os.fstat(channel.fileno())
            code = """
import json,os,sys
fd=int(sys.argv[1]); message=os.read(fd,8).decode(); os.write(fd,b'confirmed')
fds={}
for name in os.listdir('/proc/self/fd'):
 try: fds[name]=os.readlink('/proc/self/fd/'+name)
 except FileNotFoundError: pass
print(json.dumps({'message':message,'fds':fds,'inode':os.fstat(fd).st_ino}))
"""
            with self.sandbox.prepare([sys.executable, "-I", "-S", "-c", code, "placeholder"], self.work, {},
                                      provider_transport={"fd": channel.fileno()}) as launch:
                descriptor = launch.provider_fd
                self.assertIsNotNone(descriptor)
                self.assertNotEqual(descriptor, channel.fileno())
                launch.argv[-1] = str(descriptor)
                # Reuse the caller's descriptor after prepare.  The owned dup
                # must retain the exact admitted channel, not the replacement.
                channel.close()
                replacement, replacement_peer = socket.socketpair()
                with replacement, replacement_peer:
                    replacement.sendall(b"unwanted")
                    result = subprocess.run(launch.argv, env=launch.env, pass_fds=launch.pass_fds,
                                            close_fds=True, capture_output=True, text=True, timeout=8)
                self.assertEqual(0, result.returncode, result.stderr)
                observed = json.loads(result.stdout)
                self.assertEqual("provider", observed["message"])
                self.assertEqual(original.st_ino, observed["inode"])
                self.assertEqual({"0", "1", "2", str(descriptor)}, set(observed["fds"]))
                self.assertEqual(b"confirmed", server.recv(9))
                policy = launch.evidence["policy"]
                self.assertEqual("brokered-provider-v1", policy["network"])
                self.assertEqual("xlj-brokered-v1", policy["seccomp"])
                self.assertEqual(original.st_ino, policy["provider_transport"]["inode"])
                self.assertEqual(3, policy["provider_transport"]["guest_fd"])
                self.assertNotIn("fd", policy["provider_transport"])
            with self.assertRaises(OSError) as caught:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, caught.exception.errno)

    def test_provider_policy_digest_does_not_depend_on_duplicate_fd_number(self):
        server, channel = socket.socketpair()
        with server, channel:
            duplicate = os.dup(channel.fileno())
            try:
                with self.sandbox.prepare([sys.executable], self.work, {},
                                          provider_transport={"fd": channel.fileno()}) as first, \
                     self.sandbox.prepare([sys.executable], self.work, {},
                                          provider_transport={"fd": duplicate}) as second:
                    self.assertNotEqual(first.provider_fd, second.provider_fd)
                    self.assertEqual(first.evidence["policy_digest"], second.evidence["policy_digest"])
            finally:
                os.close(duplicate)

    def test_provider_loopback_is_private_and_capabilities_stay_dropped(self):
        server, channel = socket.socketpair()
        with server, channel, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as outside:
            outside.bind(("127.0.0.1", 0))
            outside.listen(1)
            port = outside.getsockname()[1]
            parent_network = os.readlink("/proc/self/ns/net")
            code = """
import ctypes,errno,fcntl,json,os,pathlib,socket,struct
status=dict(line.split(':',1) for line in pathlib.Path('/proc/self/status').read_text().splitlines() if ':' in line)
# The same address is already listening in the controller's network namespace.
with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as local:
 local.bind(('127.0.0.1',PORT)); local.listen(1)
 with socket.create_connection(local.getsockname(),timeout=1) as client:
  peer,_=local.accept()
  with peer: peer.sendall(b'private'); response=client.recv(7).decode()
denied={}
for family in ('AF_INET6','AF_NETLINK','AF_PACKET','AF_VSOCK'):
 try: sock=socket.socket(getattr(socket,family)); sock.close(); denied[family]=False
 except OSError as exc: denied[family]=exc.errno==errno.EPERM
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as control:
 flags=struct.unpack('16sh',fcntl.ioctl(control,0x8913,struct.pack('16sh',b'lo',0)+b'\\0'*22)[:18])[1]
 try: fcntl.ioctl(control,0x8914,struct.pack('16sh',b'lo',flags & ~1)+b'\\0'*22); denied['change_interface']=False
 except OSError as exc: denied['change_interface']=exc.errno==errno.EPERM
with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as direct:
 direct.settimeout(1)
 try: direct.connect(('192.0.2.1',9)); denied['external_route']=False
 except OSError as exc: denied['external_route']=exc.errno==errno.ENETUNREACH
libc=ctypes.CDLL(None,use_errno=True)
for name,call in {'ptrace':lambda:libc.ptrace(0,0,0,0),'unshare':lambda:libc.unshare(0x10000000),
                  'setns':lambda:libc.setns(-1,0),'mount':lambda:libc.mount(None,None,None,0,None)}.items():
 ctypes.set_errno(0); result=call(); denied[name]=result==-1 and ctypes.get_errno()==errno.EPERM
interfaces=[line.split(':',1)[0].strip() for line in pathlib.Path('/proc/net/dev').read_text().splitlines() if ':' in line]
print(json.dumps({'response':response,'denied':denied,'interfaces':interfaces,'loopback_up':bool(flags&1),
 'routes':pathlib.Path('/proc/net/route').read_text().splitlines()[1:],'netns':os.readlink('/proc/self/ns/net'),
 'caps':[status[name].strip() for name in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')],
 'nnp':status['NoNewPrivs'].strip(),'seccomp':status['Seccomp'].strip()}))
""".replace("PORT", str(port))
            observed, _ = self.execute(code, provider_transport={"fd": channel.fileno()}, write_scope=[])
            self.assertEqual("private", observed["response"])
            self.assertTrue(all(observed["denied"].values()), observed)
            self.assertEqual(["lo"], observed["interfaces"])
            self.assertTrue(observed["loopback_up"])
            self.assertFalse(any(row.strip() for row in observed["routes"]))
            self.assertNotEqual(parent_network, observed["netns"])
            self.assertTrue(all(int(value, 16) == 0 for value in observed["caps"]))
            self.assertEqual("1", observed["nnp"])
            self.assertEqual("2", observed["seccomp"])

    def test_provider_rejects_non_pair_or_ambient_descriptors(self):
        server, channel = socket.socketpair()
        datagram, datagram_peer = socket.socketpair(type=socket.SOCK_DGRAM)
        pipe_read, pipe_write = os.pipe()
        try:
            with server, channel, datagram, datagram_peer, \
                 socket.socket(socket.AF_INET, socket.SOCK_STREAM) as network, \
                 open(self.secret, "rb") as file:
                invalid = [{}, {"fd": -1}, {"fd": 0}, {"fd": True}, {"fd": "3"},
                           {"fd": channel.fileno(), "socket_path": "/tmp/ambient"},
                           {"fd": file.fileno()}, {"fd": pipe_read},
                           {"fd": network.fileno()}, {"fd": datagram.fileno()}]
                for value in invalid:
                    with self.subTest(value=value), self.assertRaises(Fault):
                        self.sandbox.prepare([sys.executable], self.work, {}, provider_transport=value)
                channel.bind(str(self.root / "bound-provider.sock"))
                with self.assertRaises(Fault):
                    self.sandbox.prepare([sys.executable], self.work, {},
                                         provider_transport={"fd": channel.fileno()})
                closed = os.dup(server.fileno())
                os.close(closed)
                with self.assertRaises(Fault):
                    self.sandbox.prepare([sys.executable], self.work, {}, provider_transport={"fd": closed})
        finally:
            os.close(pipe_read)
            os.close(pipe_write)

    def test_home_files_are_private_readonly_and_ancestors_cannot_be_replaced(self):
        content = "synthetic-local-config-marker"
        files = [{"path": ".codex/config.toml", "content": content},
                 {"path": ".zcode/v2/provider_config.json", "content": "{}"}]
        code = """
import json,os,pathlib,stat
home=pathlib.Path(os.environ['HOME']); config=home/'.codex/config.toml'; denied={}
for name,operation in {'write':lambda:config.write_text('changed'),'unlink':config.unlink,
 'chmod':lambda:config.chmod(0o777),'rename':lambda:config.rename(home/'replaced'),
 'ancestor':lambda:(home/'.codex').rename(home/'moved'),
 'nested_ancestor':lambda:(home/'.zcode/v2').rename(home/'.zcode/old'),
 'top_ancestor':lambda:(home/'.zcode').rename(home/'moved-zcode')}.items():
 try: operation(); denied[name]=False
 except OSError: denied[name]=True
(home/'.codex/cache').write_text('private writable cache')
print(json.dumps({'denied':denied,'content':config.read_text(),'mode':stat.S_IMODE(config.stat().st_mode),
 'readonly':bool(os.statvfs(config).f_flag&os.ST_RDONLY),
 'cache':(home/'.codex/cache').read_text(),'zcode':(home/'.zcode/v2/provider_config.json').read_text()}))
"""
        observed, evidence = self.execute(code, home_files=files)
        self.assertTrue(all(observed["denied"].values()), observed)
        self.assertEqual(content, observed["content"])
        self.assertEqual(0o600, observed["mode"])
        self.assertTrue(observed["readonly"])
        self.assertEqual("private writable cache", observed["cache"])
        self.assertEqual("{}", observed["zcode"])
        self.assertFalse((self.work / ".codex").exists())
        self.assertNotIn(content, json.dumps(evidence))
        metadata = evidence["policy"]["home_files"][0]
        self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), metadata["sha256"])
        self.assertEqual(len(content.encode()), metadata["size"])
        self.assertIn(PRIVATE_HOME + "/.codex/config.toml", evidence["policy"]["readonly_mounts"])
        self.assertIn(PRIVATE_HOME + "/.codex", evidence["policy"]["writable_mounts"])

    def test_home_file_grants_fail_closed_for_overlaps_traversal_and_size(self):
        invalid = [[{"path": path, "content": "x"}] for path in
                   ("", "/etc/config", "../escape", ".codex/../config", ".codex//config", ".codex/./config",
                    ".git/config", "tmp/config", ".codex/", "a\\b", "nul\x00name", "a" * 256)]
        invalid.extend([
            [{"path": "a", "content": "x"}, {"path": "a", "content": "y"}],
            [{"path": "a", "content": "x"}, {"path": "a/b", "content": "y"}],
            [{"path": "a/b", "content": "x"}, {"path": "a", "content": "y"}],
            [{"path": "a", "content": "x", "mode": 0o600}],
            [{"path": "a", "content": b"not text"}],
            [{"path": "a", "content": "\ud800"}],
            [{"path": str(index), "content": ""} for index in range(17)],
            [{"path": "a", "content": "x" * MAX_HOME_BYTES}, {"path": "b", "content": "x"}],
            {"path": "a", "content": "wrong outer type"},
        ])
        for index, files in enumerate(invalid):
            with self.subTest(case=index), self.assertRaises(Fault):
                self.sandbox.prepare([sys.executable], self.work, {}, home_files=files)

    def test_absent_extensions_preserve_the_default_offline_policy(self):
        with self.sandbox.prepare([sys.executable], self.work, {}) as default, \
             self.sandbox.prepare([sys.executable], self.work, {}, provider_transport=None, home_files=[]) as explicit:
            self.assertEqual(default.evidence, explicit.evidence)
            self.assertIsNone(default.provider_fd)
            self.assertNotIn("provider_transport", default.evidence["policy"])
            self.assertNotIn("home_files", default.evidence["policy"])
            self.assertEqual("none", default.evidence["policy"]["network"])


class SandboxFailClosedTests(unittest.TestCase):
    def test_source_discovery_uses_only_metadata_and_includes_required_prefixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            runtime.mkdir()
            alias = root / "runtime-alias"
            alias.symlink_to(runtime, target_is_directory=True)
            data = root / "runtime-data"
            data.write_text("contents must not be read by source discovery")
            optional = root / "missing-optional"
            required = root / "missing-python-prefix"
            with patch("xiaolvji.sandbox._TOOLCHAIN", (str(alias), str(data), str(optional))), \
                 patch("xiaolvji.sandbox.sys.base_prefix", str(alias)), \
                 patch("xiaolvji.sandbox.sys.prefix", str(required)), \
                 patch.object(LinuxSandbox, "_tools", side_effect=AssertionError("must not probe tools")), \
                 patch("xiaolvji.sandbox.subprocess.run", side_effect=AssertionError("must not start a process")), \
                 patch.object(Path, "read_bytes", side_effect=AssertionError("must not read file contents")), \
                 patch.object(Path, "read_text", side_effect=AssertionError("must not read file contents")):
                sources = readable_host_sources()
                self.assertEqual(sources, readable_host_sources())
            self.assertTrue({runtime, data, required} <= set(sources))
            self.assertNotIn(alias, sources)
            self.assertNotIn(optional, sources)
            self.assertEqual(1, sources.count(runtime))
            self.assertFalse(required.exists())

    def test_source_discovery_cannot_hide_a_host_root_runtime_grant(self):
        with patch("xiaolvji.sandbox.sys.prefix", "/"), self.assertRaises(Fault) as caught:
            readable_host_sources()
        self.assertEqual("CAPABILITY_UNSUPPORTED", caught.exception.code)

    def test_unknown_syscall_architecture_is_rejected(self):
        with self.assertRaises(Fault):
            seccomp_filter("unknown-architecture")

    def test_old_bwrap_is_not_accepted_by_version_probe(self):
        old = subprocess.CompletedProcess([], 0, stdout="bubblewrap 0.11.2\n", stderr="")
        with patch("xiaolvji.sandbox._trusted_tool", return_value=Path("/usr/bin/true")), \
             patch("xiaolvji.sandbox.subprocess.run", return_value=old), \
             self.assertRaises(Fault) as caught:
            LinuxSandbox("/nonexistent-controller")._tools()
        self.assertIn("0.12.0", str(caught.exception))

    def test_probe_reports_failure_without_a_cooperative_fallback(self):
        with patch.object(LinuxSandbox, "prepare", side_effect=Fault("CAPABILITY_UNSUPPORTED", "blocked namespace")):
            observed = LinuxSandbox("/nonexistent-controller").probe()
        self.assertFalse(observed["supported"])
        self.assertFalse(observed["strict_supported"])
        self.assertEqual(["blocked namespace"], observed["blocking_reasons"])


if __name__ == "__main__":
    unittest.main()
