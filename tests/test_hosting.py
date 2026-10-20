import base64
import json
import socket
import subprocess
import os
import re
import sys
import tempfile
import time
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app_db
import hosting
import hosting_runtimes
import hosting_registry
from server import FileHandler


class HostingUnitTests(unittest.TestCase):
    def test_public_docker_hub_reference_normalization_and_registry_allowlist(self):
        self.assertEqual(hosting_registry._hub_repository('nginx:alpine'), ('library/nginx', 'alpine'))
        self.assertEqual(hosting_registry._hub_repository('docker.io/rajputshashank/sum-server:latest'), ('rajputshashank/sum-server', 'latest'))
        self.assertIsNone(hosting_registry._hub_repository('127.0.0.1:5000/team/app:latest'))
        self.assertIsNone(hosting_registry._hub_repository('https://attacker.invalid/image:latest'))

    def test_manifest_preflight_accepts_arm64_index_and_rejects_amd64_image(self):
        multi={'mediaType':'application/vnd.oci.image.index.v1+json','manifests':[
            {'platform':{'os':'linux','architecture':'amd64'}},
            {'platform':{'os':'linux','architecture':'arm64','variant':'v8'}}]}
        with patch.object(hosting_registry,'_read_json',return_value=(multi,{})):
            result=hosting_registry.inspect_public_docker_hub_image('nginx:alpine','aarch64')
        self.assertEqual(result['architecture'],'arm64')
        self.assertEqual(len(result['platforms']),2)
        manifest={'mediaType':'application/vnd.docker.distribution.manifest.v2+json','config':{'digest':'sha256:'+'a'*64}}
        config={'architecture':'amd64','os':'linux'}
        with patch.object(hosting_registry,'_read_json',side_effect=[(manifest,{}),(config,{})]):
            with self.assertRaisesRegex(hosting_registry.UnsupportedImage,'resolves to linux/amd64.*requires linux/arm64'):
                hosting_registry.inspect_public_docker_hub_image('rajputshashank/sum-server:latest','aarch64')

    def test_confirmed_incompatible_docker_hub_manifest_fails_before_image_pull(self):
        app_id='c'*32
        row={'id':app_id,'name':'unsupported','image_reference':'rajputshashank/sum-server:latest','runtime_alias':'myf-'+app_id[:12],
             'container_port':8080,'host_port':20020,'health_path':'/','public':False,'status':'pulling','process_pid':None}
        updates=[]
        with tempfile.TemporaryDirectory() as root, patch.object(hosting,'_runtime_storage',return_value=Path(root)), \
             patch.object(hosting.shutil,'disk_usage',return_value=type('Usage',(),{'free':10*1024**3})()), \
             patch.object(hosting,'local_oci_backend',return_value=object()), \
             patch.object(hosting,'inspect_public_docker_hub_image',side_effect=hosting_registry.UnsupportedImage('Docker Hub image has no linux/arm64 manifest.')), \
             patch.object(hosting.app_db,'update_hosted_app',side_effect=lambda _id,_owner,**kw: updates.append(kw) or row), \
             patch.object(hosting.app_db,'get_hosted_app',return_value=row), \
             patch.object(hosting,'_append_log'), patch.object(hosting.subprocess,'Popen') as popen, \
             patch.dict(hosting._JOBS,{app_id:{'status':'pulling','progress':'pulling'}}):
            hosting._run_job(app_id,{'image':row['image_reference']})
        popen.assert_not_called()
        self.assertEqual(updates[-1]['status'],'failed')
        self.assertIn('no linux/arm64 manifest',updates[-1]['error'])

    def test_image_architecture_aliases_match_device_architecture(self):
        self.assertTrue(hosting.architecture_matches('arm64', 'aarch64'))
        self.assertTrue(hosting.architecture_matches('aarch64', 'arm64'))
        self.assertTrue(hosting.architecture_matches('amd64', 'x86_64'))
        self.assertFalse(hosting.architecture_matches('amd64', 'aarch64'))
        self.assertFalse(hosting.architecture_matches('arm64', 'x86_64'))

    def test_node_source_environment_preserves_termux_exec_shim_but_isolates_home(self):
        with tempfile.TemporaryDirectory() as home, patch.object(hosting,"_SOURCES",Path(home)/"sources"), patch.object(hosting,"_SECRETS",Path(home)/"secrets"), patch.dict(os.environ,{
            "PATH":"/termux/bin:/system/bin","PREFIX":"/termux","LD_PRELOAD":"/termux/lib/libtermux-exec.so",
            "LD_LIBRARY_PATH":"/termux/lib","TERMUX_EXEC__PROC_SELF_EXE":"/termux/bin/env",
            "TERMUX__SE_PROCESS_CONTEXT":"u:r:untrusted_app:s0","TMPDIR":"/system/tmp"}):
            app_id="f"*32
            hosting._SECRETS.mkdir(parents=True,exist_ok=True)
            (hosting._SECRETS/(app_id+".json")).write_text(json.dumps({"environment":{"LD_PRELOAD":"/bad.so","HOME":"/bad","APP_MODE":"test"}}))
            env=hosting._node_environment(app_id,{"container_port":8080,"host_port":20000})
            self.assertEqual(env["LD_PRELOAD"],"/termux/lib/libtermux-exec.so")
            self.assertEqual(env["LD_LIBRARY_PATH"],"/termux/lib")
            self.assertEqual(env["TERMUX_EXEC__PROC_SELF_EXE"],"/termux/bin/env")
            self.assertEqual(env["HOME"],str(hosting.source_directory(app_id)))
            self.assertEqual(env["APP_MODE"],"test")

    def test_node_source_reference_and_configuration_validation(self):
        spec=hosting._validate_source_spec({'name':'sum-server','owner':'rajputshashank003','repository':'sum-server','branch':'main','containerPort':8080,'healthPath':'/'})
        self.assertEqual(spec['image'],'github:rajputshashank003/sum-server@main')
        self.assertEqual(spec['containerPort'],8080)
        self.assertEqual(hosting.parse_source_reference(spec['image']),{'owner':'rajputshashank003','repo':'sum-server','branch':'main'})
        for branch in ('-option','../main','main//test','main/','main;id'):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                hosting._validate_source_spec({'name':'bad','owner':'owner','repository':'repo','branch':branch})
        with self.assertRaises(ValueError): hosting._validate_source_spec({'name':'bad','owner':'owner','repository':'repo','containerPort':80})

    def test_node_source_create_records_a_distinct_runtime_without_starting_oci(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        previous=app_db.DB_PATH;app_db.DB_PATH=Path(temp.name)/'db'/'platform.sqlite3';self.addCleanup(setattr,app_db,'DB_PATH',previous)
        state=Path(temp.name)/'hosting'
        patches=[patch.object(hosting,'_STATE',state),patch.object(hosting,'_LOGS',state/'logs'),patch.object(hosting,'_SECRETS',state/'secrets'),patch.object(hosting,'_SOURCES',state/'sources')]
        for item in patches:item.start()
        self.addCleanup(lambda:[item.stop() for item in reversed(patches)])
        app_db.init()
        host_arch=__import__('os').uname().machine.lower()
        node_arch={'aarch64':'arm64','arm64':'arm64','x86_64':'x64','amd64':'x64','armv7l':'arm'}[host_arch]
        with patch.object(hosting,'node_toolchain',return_value={'available':True,'node':'/termux/bin/node','npm':'/termux/bin/npm','version':'v22.0.0','architecture':node_arch}), \
             patch.object(hosting,'_check_port_available'), patch.object(hosting.threading,'Thread') as thread:
            result=hosting.create_node_source({'name':'sum-server','owner':'rajputshashank003','repository':'sum-server','branch':'main','containerPort':8080,'public':True})
        self.assertEqual(result['deploymentType'],'node-source')
        self.assertEqual(result['source'],{'provider':'GitHub','owner':'rajputshashank003','repository':'sum-server','branch':'main'})
        self.assertGreaterEqual(result['host_port'],hosting.HOST_PORT_MIN)
        self.assertLessEqual(result['host_port'],hosting.HOST_PORT_MAX)
        self.assertTrue(result['public'])
        self.assertNotIn(str(state),json.dumps(result))
        row=app_db.get_hosted_app(result['id'],hosting.OWNER)
        self.assertEqual(row['runtime_alias'],'source-'+result['id'][:12])
        self.assertEqual(row['image_reference'],'github:rajputshashank003/sum-server@main')
        thread.assert_called_once()

    def test_proot_adapter_targets_native_architecture_and_validates_aliases(self):
        backend=hosting_runtimes.ProotOCIBackend("/termux/bin/proot-distro")
        self.assertEqual(backend.install_argv("myf-safe123","nginx:alpine","aarch64"),
                         ["/termux/bin/proot-distro","install","--quiet","--name","myf-safe123","--architecture","aarch64","nginx:alpine"])
        self.assertEqual(backend.run_options(),["/termux/bin/proot-distro","run","--isolated","--redirect-ports"])
        with self.assertRaises(ValueError):backend.remove_argv("../other")
        with self.assertRaises(RuntimeError):backend.native_architecture("mips")

    def test_runtime_inventory_reports_unavailable_docker_and_unconfigured_remote_truthfully(self):
        from types import SimpleNamespace
        with patch.object(hosting_runtimes,"detect_runtime",return_value=SimpleNamespace(available=True,version="5.3.0")), \
             patch.object(hosting_runtimes.shutil,"which",return_value=None), \
             patch.object(hosting_runtimes,"_namespace_probe",return_value=False):
            previous_cache=hosting_runtimes._CACHE
            hosting_runtimes._CACHE=None
            self.addCleanup(setattr,hosting_runtimes,"_CACHE",previous_cache)
            report=hosting_runtimes.runtime_inventory(force=True)
        states={item["id"]:item["status"] for item in report["backends"]}
        self.assertEqual(states["docker-engine"],"UNAVAILABLE")
        self.assertEqual(states["rootless-docker"],"UNAVAILABLE")
        self.assertEqual(states["remote-docker"],"NOT CONFIGURED")
        self.assertEqual(states["proot-oci"],"AVAILABLE")
        self.assertNotIn("secret",json.dumps(report).lower())

    def test_amd64_only_image_fails_before_entrypoint_when_host_is_arm64(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            previous=hosting._runtime_storage;hosting._runtime_storage=lambda:Path(root)
            self.addCleanup(setattr,hosting,"_runtime_storage",previous)
            alias="myf-"+"e"*12;directory=Path(root)/"containers"/alias;directory.mkdir(parents=True)
            manifest={"arch":"aarch64","image_ref":"docker.io/rajputshashank/sum-server",
                      "image_config":{"architecture":"amd64","os":"linux","config":{
                          "Entrypoint":["docker-entrypoint.sh"],"Cmd":["npm","run","start"],
                          "WorkingDir":"/app","ExposedPorts":{"8080/tcp":{}},
                      }}}
            (directory/"manifest.json").write_text(json.dumps(manifest))
            app={"id":"e"*32,"runtime_alias":alias,"image_reference":"docker.io/rajputshashank/sum-server",
                 "container_port":8080,"host_port":20002,"command":[]}
            with patch.object(hosting,"detect_runtime",return_value=SimpleNamespace(available=True,message="",executable="/termux/bin/proot-distro")), \
                 patch.object(hosting.shutil,"which",return_value=None), \
                 patch.object(hosting.subprocess,"Popen") as process:
                with self.assertRaisesRegex(RuntimeError,
                    r"Image architecture amd64 does not match device architecture aarch64.*no QEMU user-mode emulator configured"):
                    hosting._spawn(app)
                process.assert_not_called()

    def test_image_metadata_reports_architecture_and_runtime_limitations_without_env_values(self):
        with tempfile.TemporaryDirectory() as root:
            old=hosting._runtime_storage;hosting._runtime_storage=lambda:Path(root)
            self.addCleanup(setattr,hosting,"_runtime_storage",old)
            app={"id":"a"*32,"runtime_alias":"myf-image","image_reference":"example/image:tag"}
            p=Path(root)/"containers"/"myf-image";p.mkdir(parents=True)
            (p/"manifest.json").write_text(json.dumps({"arch":"aarch64","image_config":{"architecture":"amd64","config":{"WorkingDir":"/srv","Entrypoint":["run"],"Cmd":["--serve"],"ExposedPorts":{"80/tcp":{},"443/tcp":{}},"Env":["PRIVATE_KEY=do-not-return"],"Healthcheck":{"Test":["CMD","check"]},"Volumes":{"/data":{}}}}}))
            details=hosting._deployment_runtime_details(app)
        self.assertEqual(details["runtimeBackend"],"PRoot-Distro OCI")
        self.assertEqual(details["imageArchitecture"],"amd64")
        self.assertFalse(details["architectureCompatible"])
        self.assertEqual(details["imageConfiguration"]["exposedPorts"],["443/tcp","80/tcp"])
        self.assertEqual(details["imageConfiguration"]["environmentKeys"],["PRIVATE_KEY"])
        self.assertNotIn("do-not-return",json.dumps(details))
        self.assertTrue(any("QEMU" in message for message in details["unsupportedRequirements"]))

    def test_image_port_autodetect_requires_single_tcp_expose(self):
        app={"id":"a"*32,"container_port":0}
        with patch.object(hosting,"_manifest",return_value={"ExposedPorts":{"6379/tcp":{}}}), \
             patch.object(hosting.app_db,"update_hosted_app",return_value={**app,"container_port":6379}) as update:
            result=hosting._resolve_image_port(app)
        self.assertEqual(result["container_port"],6379);update.assert_called_once_with("a"*32,hosting.OWNER,container_port=6379)
        with patch.object(hosting,"_manifest",return_value={"ExposedPorts":{"80/tcp":{},"443/tcp":{}}}), self.assertRaisesRegex(RuntimeError,"multiple TCP EXPOSE"):
            hosting._resolve_image_port(app)

    def test_host_port_allocator_reserves_existing_apps_and_main_services(self):
        rows=[{'id':'a'*32,'host_port':20000},{'id':'b'*32,'host_port':8000}]
        with patch.object(hosting,'_check_port_available') as probe:
            self.assertEqual(hosting._allocate_host_port(rows),20001)
            probe.assert_called_once_with(20001)
        with patch.object(hosting,'_check_port_available'):
            with self.assertRaises(ValueError):hosting._allocate_host_port(rows,requested=20000)
            with self.assertRaises(ValueError):hosting._allocate_host_port(rows,requested=8000)

    def test_standard_nginx_image_listener_is_remapped_per_deployment(self):
        with tempfile.TemporaryDirectory() as root:
            previous=hosting._runtime_storage
            storage=Path(root)/'proot';hosting._runtime_storage=lambda:storage
            self.addCleanup(setattr,hosting,'_runtime_storage',previous)
            config=storage/'containers'/'myf-nginx'/'rootfs'/'etc/nginx/conf.d/default.conf'
            config.parent.mkdir(parents=True)
            config.write_text('server {\n    listen 80;\n    listen [::]:80;\n}\n')
            changed=hosting._configure_oci_listener({'id':'c'*32,'runtime_alias':'myf-nginx','container_port':80,'host_port':20000})
            self.assertTrue(changed)
            text=config.read_text()
            self.assertIn('listen 127.0.0.1:20000;',text)
            self.assertIn('listen [::1]:20000;',text)

    def test_three_node_apps_run_concurrently_and_actions_are_isolated(self):
        tools=hosting.node_toolchain()
        if not tools['available']:self.skipTest('Termux Node.js/npm are unavailable on this test host.')
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);root=Path(temp.name)
        previous=app_db.DB_PATH;app_db.DB_PATH=root/'db'/'platform.sqlite3';self.addCleanup(setattr,app_db,'DB_PATH',previous)
        replacements={'_STATE':root/'hosting','_LOGS':root/'hosting/logs','_SECRETS':root/'hosting/secrets','_SOURCES':root/'hosting/sources'}
        patches=[patch.object(hosting,key,value) for key,value in replacements.items()]
        for item in patches:item.start()
        self.addCleanup(lambda:[item.stop() for item in reversed(patches)])
        app_db.init();hosting._dirs()
        ids=[]
        js="const http=require('node:http');http.createServer((req,res)=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify({id:process.env.APP_ID,port:process.env.PORT}));}).listen(8080);\n"
        for number in range(3):
            app_id=f'{number+1:x}'*32;ids.append(app_id);source=hosting.source_directory(app_id);source.mkdir(parents=True)
            (source/'package.json').write_text(json.dumps({'name':f'app-{number}','version':'1.0.0','scripts':{'start':'node server.js'}}))
            (source/'server.js').write_text(js)
            port=hosting._allocate_host_port(app_db.list_hosted_apps(hosting.OWNER))
            private=hosting._SECRETS/(app_id+'.json')
            private.write_text(json.dumps({'environment':{'APP_ID':app_id},'command':[]}));os.chmod(private,0o600)
            app_db.create_hosted_app({'id':app_id,'owner_id':hosting.OWNER,'name':f'App {number}','image_reference':f'github:owner/repo{number}@main',
                'runtime_alias':hosting._source_alias(app_id),'container_port':8080,'host_port':port,'health_path':'/','public':True,
                'status':'stopped','log_file':str(hosting._log_path(app_id)),'command':[],'environmentKeys':['APP_ID']})
        def wait_running(app_id,expected):
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                row=app_db.get_hosted_app(app_id,hosting.OWNER)
                if row and row['status']=='running':return row
                if row and row['status']=='failed':raise AssertionError(row['error'])
                time.sleep(.1)
            raise AssertionError(f'{app_id} did not become ready')
        proxy=ThreadingHTTPServer(('127.0.0.1',0),FileHandler);proxy.password='hosting-test-password'
        proxy_thread=threading.Thread(target=proxy.serve_forever,daemon=True);proxy_thread.start()
        self.addCleanup(proxy.shutdown);self.addCleanup(proxy.server_close);self.addCleanup(proxy_thread.join,2)
        def verify(app_id):
            row=app_db.get_hosted_app(app_id,hosting.OWNER)
            with urlopen(f"http://127.0.0.1:{row['host_port']}/",timeout=2) as response:
                direct=json.loads(response.read())
            with urlopen(f"http://127.0.0.1:{proxy.server_port}/hosted/{app_id}/",timeout=2) as response:
                routed=json.loads(response.read())
            expected={'id':app_id,'port':str(row['host_port'])}
            self.assertEqual(direct,expected);self.assertEqual(routed,expected)
        try:
            for app_id in ids:hosting.start_application(app_id)
            rows=[wait_running(app_id,True) for app_id in ids]
            self.assertEqual(len({row['host_port'] for row in rows}),3)
            self.assertNotIn(8000,{row['host_port'] for row in rows})
            for app_id in ids:verify(app_id)
            with patch.object(hosting,'_RECOVERY_DONE',False):
                self.assertEqual(hosting.recover_applications(),[])
            for app_id in ids:verify(app_id)
            hosting.stop_application(ids[0])
            self.assertEqual(app_db.get_hosted_app(ids[0],hosting.OWNER)['status'],'stopped')
            verify(ids[1]);verify(ids[2])
            hosting.restart_application(ids[1]);wait_running(ids[1],True)
            verify(ids[1]);verify(ids[2])
            self.assertEqual(app_db.get_hosted_app(ids[0],hosting.OWNER)['status'],'stopped')
        finally:
            for app_id in ids:
                try:hosting.stop_application(app_id)
                except Exception:pass
                with hosting._LOCK:
                    hosting._PROCESSES.pop(app_id,None);hosting._JOBS.pop(app_id,None);hosting._APP_LOCKS.pop(app_id,None)

    def test_image_and_app_configuration_validation(self):
        spec=hosting._validate_spec({'name':'Demo app','image':'nginx:alpine','containerPort':80,'environment':{'MODE':'demo'}})
        self.assertEqual(spec['hostPort'],2080)
        self.assertEqual(spec['environment'],{'MODE':'demo'})
        for ref in ('-bad:tag','nginx:','x@sha256:nope','../nginx:latest','nginx image'):
            with self.subTest(ref=ref), self.assertRaises(ValueError): hosting._valid_image(ref)
        with self.assertRaises(ValueError): hosting._validate_spec({'name':'bad','image':'nginx:alpine','containerPort':8000,'command':'echo hi'})
        with self.assertRaises(ValueError): hosting._validate_spec({'name':'bad','image':'nginx:alpine','containerPort':8000,'environment':{'A':'x\x00y'}})

    def test_public_serialization_never_contains_environment_values(self):
        value={'id':'a'*32,'name':'x','image_reference':'nginx:alpine','container_port':80,'host_port':2080,'health_path':'/','status':'stopped','public':False,'error':'','created_at':1,'updated_at':2,'process_pid':None,'command':[],'environmentKeys':['MYVAR'],'runtime_alias':'myf-'+('a'*12),'process_start_ticks':None,'log_file':'/private/path'}
        with patch.object(hosting,'_alive_status',return_value=False):
            result=hosting.public_app(value)
        self.assertNotIn('environment',result)
        self.assertNotIn('sensitive-value',json.dumps(result))
        self.assertNotIn('/private/path',json.dumps(result))


    def test_hosted_metadata_survives_repeatable_database_initialization(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        previous=app_db.DB_PATH
        app_db.DB_PATH=Path(temp.name)/'db'/'platform.sqlite3'
        self.addCleanup(setattr,app_db,'DB_PATH',previous)
        app_db.init()
        app_db.create_hosted_app({'id':'c'*32,'owner_id':'local-admin','name':'Persistent','image_reference':'nginx:alpine','runtime_alias':'myf-persistent','container_port':80,'host_port':2080,'health_path':'/','log_file':'/private/log','status':'stopped','command':[],'environmentKeys':['TOKEN']})
        app_db.init()
        row=app_db.get_hosted_app('c'*32,'local-admin')
        self.assertEqual(row['image_reference'],'nginx:alpine')
        self.assertEqual(row['status'],'stopped')
        self.assertEqual(row['command'],[])
        self.assertEqual(app_db.health()['hostedApps'],1)

    def test_standalone_log_sink_redacts_values_and_rotates(self):
        with tempfile.TemporaryDirectory() as home:
            root=Path(home)/'.local/share/myforever/hosting'
            (root/'secrets').mkdir(parents=True);(root/'logs').mkdir()
            (root/'secrets'/('d'*32+'.json')).write_text(json.dumps({'environment':{'API_TOKEN':'sensitive-log-value'},'command':[]}))
            env=os.environ.copy();env['HOME']=home
            result=subprocess.run([sys.executable,str(Path(hosting.__file__).with_name('hosting_log_writer.py')),'d'*32],
                                  input='runtime says sensitive-log-value\n',text=True,capture_output=True,env=env,timeout=5)
            self.assertEqual(result.returncode,0)
            log=(root/'logs'/('d'*32+'.log')).read_text()
            self.assertIn('[REDACTED]',log);self.assertNotIn('sensitive-log-value',log)
            self.assertEqual((root/'logs'/('d'*32+'.log')).stat().st_mode & 0o777,0o600)

    def test_controlled_wrapper_passes_arguments_without_shell_interpolation(self):
        config={'Entrypoint':['/docker-entrypoint.sh'],'Cmd':['nginx','-g','daemon off;']}
        app={'command':['node','hello world.js; touch /tmp/not-run']}
        argv=hosting._entrypoint(config,app)
        self.assertEqual(argv,['/docker-entrypoint.sh','node','hello world.js; touch /tmp/not-run'])
        self.assertEqual(hosting._guest_env({'Env':['PATH=/bin','A=default']},{'A':'override'}),{'PATH':'/bin','A':'override'})


class HostedProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); root=Path(self.temp.name)
        self.db_patch=patch.object(app_db,'DB_PATH',root/'db'/'platform.sqlite3');self.db_patch.start();app_db.init()
        self.state_patch=patch.object(hosting,'_STATE',root/'hosting');self.state_patch.start()
        hosting._dirs()
        self.child=subprocess.Popen(['python','-c','import time; time.sleep(20)'])
        self.body=b'HOSTED_ROUTE_OK'
        body=self.body
        class AppHandler(BaseHTTPRequestHandler):
            def do_GET(inner):
                inner.send_response(200);inner.send_header('Content-Length',str(len(body)));inner.end_headers();inner.wfile.write(body)
            def log_message(inner,*args):pass
        self.upstream=ThreadingHTTPServer(('127.0.0.1',0),AppHandler)
        self.upstream_thread=threading.Thread(target=self.upstream.serve_forever,daemon=True);self.upstream_thread.start()
        self.app_id='b'*32
        app_db.create_hosted_app({'id':self.app_id,'owner_id':hosting.OWNER,'name':'Proxy test','image_reference':'test:unit','runtime_alias':'myf-'+self.app_id[:12],
          'container_port':8080,'host_port':self.upstream.server_port,'health_path':'/','status':'running','public':True,'log_file':str(root/'logs')})
        app_db.update_hosted_app(self.app_id,hosting.OWNER,status='running',process_pid=self.child.pid,process_start_ticks=hosting._ticks(self.child.pid))
        self.proc_patch=patch.dict(hosting._PROCESSES,{self.app_id:self.child});self.proc_patch.start()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),FileHandler);self.server.password='test-password'
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.upstream.shutdown();self.upstream.server_close()
        self.proc_patch.stop()
        self.child.terminate();self.child.wait(timeout=3)
        self.state_patch.stop();self.db_patch.stop();self.temp.cleanup()

    def test_explicit_public_route_proxies_to_only_registered_loopback_target_without_basic_auth(self):
        with urlopen(self.base+f'/hosted/{self.app_id}/hello',timeout=3) as response:
            self.assertEqual(response.status,200);self.assertEqual(response.read(),self.body)

    def test_private_app_route_requires_existing_authentication(self):
        app_db.update_hosted_app(self.app_id,hosting.OWNER,public=False)
        with self.assertRaises(HTTPError) as error:urlopen(self.base+f'/hosted/{self.app_id}/',timeout=3)
        self.assertEqual(error.exception.code,401)

    def test_hosting_api_requires_existing_authentication(self):
        with self.assertRaises(HTTPError) as error:urlopen(self.base+'/api/hosting/apps',timeout=3)
        self.assertEqual(error.exception.code,401)

    def test_node_source_management_route_requires_auth_and_uses_existing_controller(self):
        body=json.dumps({'name':'sum-server','owner':'rajputshashank003','repository':'sum-server','branch':'main','containerPort':8080}).encode()
        request=Request(self.base+'/api/hosting/source',data=body,method='POST',headers={'Content-Type':'application/json'})
        with patch('hosting.create_node_source') as create:
            with self.assertRaises(HTTPError) as error:urlopen(request,timeout=3)
            self.assertEqual(error.exception.code,401)
            create.assert_not_called()
            token='Basic '+base64.b64encode(b'admin:test-password').decode()
            request=Request(self.base+'/api/hosting/source',data=body,method='POST',headers={'Authorization':token,'Content-Type':'application/json'})
            create.return_value={'id':'e'*32,'deploymentType':'node-source'}
            with urlopen(request,timeout=3) as response:
                self.assertEqual(response.status,202)
                self.assertEqual(json.loads(response.read()),create.return_value)
            create.assert_called_once_with(json.loads(body))

    def test_dedicated_hosting_pages_are_authenticated_and_refreshable(self):
        token='Basic '+base64.b64encode(b'admin:test-password').decode()
        routes=('/host','/host/new','/host/existing','/deployed/'+('a'*32))
        for route in routes:
            with self.subTest(route=route):
                with self.assertRaises(HTTPError) as denied:urlopen(self.base+route,timeout=3)
                self.assertEqual(denied.exception.code,401)
                with urlopen(Request(self.base+route,headers={'Authorization':token}),timeout=3) as response:
                    self.assertEqual(response.status,200)
                    page=response.read();self.assertIn(b'id=\"hostMain\"',page);self.assertNotIn(b'id=\"managerCard\"',page);self.assertNotIn(b'id=\"filesTab\"',page);self.assertIn(b'Hosting navigation',page);self.assertIn(b'id="hostMenuToggle"',page);self.assertIn(b'id="menuHomeLink"',page);self.assertNotIn(b'Back to MyFile',page);
                    menu=re.search(rb'<div id="globalMenu".*?</div>\s*<nav class="host-nav"',page,re.S)
                    self.assertIsNotNone(menu)
                    menu=menu.group(0)
                    for link_id in (b'menuHomeLink',b'menuAgenticLink',b'menuHostLink',b'themeMenuToggle'):
                        self.assertIn(link_id,menu)
                    for removed in (b'data-main-section',b'Stream',b'Clipboard',b'Direct Hosting',b'Health Endpoint',b'Data Explorer',b'Logs'):
                        self.assertNotIn(removed,menu)
                    self.assertIn(b"document.querySelector('#menuHostLink')?.classList.add('hidden')",page)
                    self.assertIn(b"document.querySelector('#menuHomeLink')?.classList.remove('hidden')",page)

    def test_invalid_deployment_identifier_is_a_clear_api_not_found(self):
        token='Basic '+base64.b64encode(b'admin:test-password').decode()
        with self.assertRaises(HTTPError) as error:urlopen(Request(self.base+'/api/hosting/apps/not-a-deployment-id',headers={'Authorization':token}),timeout=3)
        self.assertEqual(error.exception.code,404)

    def test_host_tab_capability_and_status_are_served_by_existing_handler(self):
        token='Basic '+base64.b64encode(b'admin:test-password').decode()
        with urlopen(Request(self.base+'/api/capabilities',headers={'Authorization':token}),timeout=3) as response:
            self.assertTrue(json.loads(response.read())['hosting'])
        with urlopen(Request(self.base+'/api/hosting/status',headers={'Authorization':token}),timeout=3) as response:
            status=json.loads(response.read())
            self.assertIn('runtime',status);self.assertIn('security',status)
        with urlopen(Request(self.base+'/',headers={'Authorization':token}),timeout=3) as response:
            page=response.read();self.assertNotIn(b'id="hostTab"',page);self.assertIn(b'id="menuHostLink"',page);self.assertIn(b'id="globalMenu"',page);self.assertIn(b'id="homeMenuToggle"',page);self.assertIn(b'href="/host"',page);self.assertNotIn(b'id="hostMain"',page);self.assertNotIn(b'id="hostPanel"',page)
            menu=re.search(rb'<div id="globalMenu".*?<div id="modal"',page,re.S)
            self.assertIsNotNone(menu)
            menu=menu.group(0)
            for link_id in (b'menuAgenticLink',b'menuHostLink',b'menuHomeLink',b'themeMenuToggle'):
                self.assertIn(link_id,menu)
            for removed in (b'data-main-section',b'Stream',b'Clipboard',b'Direct Hosting',b'Health Endpoint',b'Data Explorer',b'Logs'):
                self.assertNotIn(removed,menu)
            self.assertIn(b"homeLink.classList.toggle('hidden',onHome)",page)
        with urlopen(Request(self.base+'/host',headers={'Authorization':token}),timeout=3) as response:
            page=response.read();self.assertIn(b'id="hostMain"',page);self.assertNotIn(b'id="managerCard"',page);self.assertNotIn(b'id="filesTab"',page);self.assertIn(b'href="/host/new"',page);self.assertIn(b'href="/host/existing"',page)


if __name__=='__main__': unittest.main()
