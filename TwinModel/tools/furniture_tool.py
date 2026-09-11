"""Plan or interactively author grouped furniture from a baked .twin.

python tools/furniture_tool.py --twin out/v10_eixample/eixample.twin \
  --output out/furniture_eixample --poles /path/to/placements.json \
  --vegetation /path/to/vegetation-plan.json --serve

Open localhost:8795. Optionally supply --level, --project and --engine to enable
Bake from the UI. Every change is validated before replacing the saved plan.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shapely.geometry import mapping
from twinmodel.model import TwinModel
from twinmodel.furniture import configuration, plan


def atomic_json(path, value):
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')
    temp.replace(path)


class Workspace:
    def __init__(self,args, *, restore=False):
        self.args=args
        self.model=TwinModel.load(args.twin)
        self.output=Path(args.output).resolve();self.output.mkdir(parents=True,exist_ok=True)
        self.config_path=self.output/'config.json'
        cfg=Path(args.config) if args.config else self.config_path
        self.config=configuration(json.loads(cfg.read_text()) if cfg.exists() else {})
        if getattr(args,'anchors',None):
            imported=json.loads(Path(args.anchors).read_text())
            self.config['anchors']=imported['anchors']
            self.config=configuration(self.config)
        self.poles=[]
        if self.model.signals and not args.poles:
            raise ValueError('This map has traffic controls. Supply --poles with their validated physical placements.')
        if args.poles:
            raw=json.loads(Path(args.poles).read_text())
            for s in raw:
                p=s.get('placement',s)
                if p.get('status','ok')!='ok':raise ValueError('Unresolved pole input')
                self.poles.append(p)
        self.vegetation=json.loads(Path(args.vegetation).read_text()) if args.vegetation else None
        if self.model.objects and not args.vegetation:
            raise ValueError('Supply --vegetation with the baked vegetation plan to reserve existing plants and tree bases.')
        self.other_plans=[json.loads(Path(p).read_text()) for p in args.occupied_plan]
        self.lock=threading.RLock();self.process=None;self.log_file=None
        saved_plan = self.output/'plan.json'
        if restore and saved_plan.exists():
            self.result = json.loads(saved_plan.read_text())
            self.config = configuration(self.result['config'])
            self.seconds = 0.0
        else:
            self.regenerate(self.config)
        self.geometry={'surfaces':[{'kind':s.kind,'geometry':mapping(s.geometry)} for s in self.model.surfaces],
                       'buildings':[mapping(b.footprint) for b in self.model.buildings],
                       'poles':[[p['x'],-p['y']] for p in self.poles]}

    def regenerate(self,cfg):
        with self.lock:
            if self.process and self.process.poll() is None:raise ValueError('Bake is running; wait before changing its plan')
            started=time.monotonic();result=plan(self.model,cfg,self.poles,self.vegetation,self.other_plans)
            result['region']=self.args.region
            result['source_twin']=str(Path(self.args.twin).resolve())
            atomic_json(self.output/'plan.json',result)
            atomic_json(self.config_path,result['config'])
            self.config=result['config'];self.result=result;self.seconds=time.monotonic()-started
            return self.state()

    def state(self):
        with self.lock:
            bake={'enabled':bool(self.args.engine and self.args.project and self.args.level),'running':False}
            if self.process:
                code=self.process.poll();bake.update(running=code is None,exit_code=code)
                report=self.output/'bake-report.json'
                if code==0 and report.exists():bake['report']=json.loads(report.read_text())
                log=self.output/'bake.log'
                if log.exists():
                    log_text=log.read_text(errors='replace')
                    bake['log_tail']=log_text[-4000:]
                    errors=[line.split('RuntimeError:',1)[1].strip() for line in log_text.splitlines() if 'RuntimeError:' in line]
                    if errors:bake['error']=errors[-1]
            display=self.result
            validated=self.output/'validated-plan.json'
            if validated.exists():
                saved=json.loads(validated.read_text())
                if saved.get('source_plan_sha256')==hashlib.sha256((self.output/'plan.json').read_bytes()).hexdigest():
                    display=saved
                    report=self.output/'bake-report.json'
                    if report.exists():bake['report']=json.loads(report.read_text())
            return {'config':self.config,'plan':display,'seconds':self.seconds,'bake':bake}

    def bake(self):
        with self.lock:
            if not all((self.args.engine,self.args.project,self.args.level)):
                raise ValueError('Start with --engine, --project and --level to enable baking')
            if self.process and self.process.poll() is None:raise ValueError('Bake already running')
            script=Path(__file__).resolve().parents[1]/'ue/bake_furniture.py'
            import shlex
            script_args=shlex.join([str(script),'--name',self.args.level,'--plan',str(self.output/'plan.json'),
                                   '--region',self.args.region,'--report',str(self.output/'bake-report.json')])
            command=[str(Path(self.args.engine).resolve()),str(Path(self.args.project).resolve()),
                     '-run=pythonscript','-script='+script_args,'-nullrhi','-unattended','-nosound']
            if self.log_file:self.log_file.close()
            self.log_file=open(self.output/'bake.log','w')
            report=self.output/'bake-report.json'
            if report.exists():report.unlink()
            self.process=subprocess.Popen(command,stdout=self.log_file,stderr=subprocess.STDOUT)
            return self.state()


def serve(workspace,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,value,status=200):
            body=json.dumps(value,allow_nan=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            if self.path=='/state':return self.reply(workspace.state())
            if self.path=='/geometry':return self.reply(workspace.geometry)
            if self.path=='/':
                body=Path(__file__).with_name('furniture_tool.html').read_bytes()
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            self.reply({'error':'Not found'},404)
        def do_POST(self):
            try:
                # Local authoring endpoint; reject cross-origin browser mutations.
                origin=self.headers.get('Origin')
                if origin and origin not in (f'http://localhost:{port}',f'http://127.0.0.1:{port}'):
                    return self.reply({'error':'Origin not allowed'},403)
                length=int(self.headers.get('Content-Length','0'))
                if length>2_000_000:return self.reply({'error':'Configuration too large'},413)
                data=json.loads(self.rfile.read(length) or b'{}')
                if self.path=='/preview':return self.reply(workspace.regenerate(data))
                if self.path=='/bake':return self.reply(workspace.bake())
                return self.reply({'error':'Not found'},404)
            except (ValueError,KeyError,TypeError,OSError) as e:self.reply({'error':str(e)},400)
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Furniture tool: http://localhost:{port}',flush=True)
    server.serve_forever()


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--twin',required=True);ap.add_argument('--output',required=True)
    ap.add_argument('--anchors');ap.add_argument('--vegetation');ap.add_argument('--occupied-plan',action='append',default=[]);ap.add_argument('--config');ap.add_argument('--poles');ap.add_argument('--serve',action='store_true')
    ap.add_argument('--port',type=int,default=8795)
    ap.add_argument('--level');ap.add_argument('--project');ap.add_argument('--engine');ap.add_argument('--region',default='main')
    args=ap.parse_args();workspace=Workspace(args)
    print(json.dumps(workspace.result['summary']),flush=True)
    if args.serve:serve(workspace,args.port)

if __name__=='__main__':main()
