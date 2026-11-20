#!/usr/bin/env python3
"""Package NickLabs AI on Garlic LLM as an installable PKG (title NLNA00001, separate from the NLWK00001 worker). No weights, no credentials."""
import os,shutil,subprocess,hashlib,json
from pathlib import Path
a=Path(__file__).resolve().parents[1];root=a.parent;w=Path(os.environ.get('NLAG_BUILD_ROOT',root/'ps4-worker'))   # w: folder with build/OpenOrbis, build/openssl-3.5.4, build/dotnet, build/pkgtool, vendor/json.hpp
GARLIC=Path(os.environ.get('GARLIC_DIR') or next((p for p in (root/'sources/garlic-llm',root.parent,root) if (p/'chat/engine.c').exists()),root/'sources/garlic-llm'))   # the Garlic LLM checkout (this folder sits inside a fork of it, or next to sources/garlic-llm)
sdk=Path(os.environ.get('OO_PS4_TOOLCHAIN',w/'build/OpenOrbis/PS4Toolchain'))
out=a/'build/package';out.mkdir(parents=True,exist_ok=True)
stage=out/'root'
if stage.exists():shutil.rmtree(stage)
stage.mkdir()
(stage/'sce_sys/about').mkdir(parents=True,exist_ok=True);(stage/'sce_module').mkdir(exist_ok=True)
shutil.copy2(a/'build/native/eboot.bin',stage/'eboot.bin')
# trust list = the worker's pinned 9B set(s) + the native 4B set (native-app/trusted-4b.json); a record can only vouch for the exact files it pins
import json as _j
_t=_j.loads((a/'trusted-assets-9b.json').read_text()) if (a/'trusted-assets-9b.json').exists() else _j.loads((w/'trusted-assets.json').read_text());_t=_t if isinstance(_t,list) else [_t]
(stage/'trusted-assets.json').write_text(_j.dumps(_t+[_j.loads((a/'trusted-4b.json').read_text())],indent=1)+'\n')
shutil.copy2(GARLIC/'LICENSE',stage/'LICENSE-Garlic.txt')
shutil.copy2(w/'build/openssl-3.5.4/LICENSE.txt',stage/'LICENSE-OpenSSL.txt')
shutil.copy2(w/'vendor/json.hpp',stage/'json-source-and-MIT-license.hpp')
shutil.copy2(GARLIC/'chat/sce_sys/icon0.png',stage/'sce_sys/icon0.png')
sample=sdk/'samples/hello_world'
shutil.copy2(sample/'sce_sys/about/right.sprx',stage/'sce_sys/about/right.sprx')
for p in (sample/'sce_module').glob('*.prx'):shutil.copy2(p,stage/'sce_module'/p.name)
cmd=[w/'build/dotnet/dotnet',w/'build/pkgtool/PkgTool.Core.dll']
env=dict(os.environ,OO_PS4_TOOLCHAIN=str(sdk))
def run(args):subprocess.run(list(map(str,args)),cwd=stage,env=env,check=True)
def pkg(*args):run(cmd+list(args))
sfo='sce_sys/param.sfo';cid='IV0000-NLNA00001_00-NICKLABSNATIVE01'
pkg('sfo_new',sfo)
for key,typ,size,value in [('APP_TYPE','Integer',4,'1'),('APP_VER','Utf8',8,'00.06'),('ATTRIBUTE','Integer',4,'0'),('CATEGORY','Utf8',4,'gd'),('CONTENT_ID','Utf8',48,cid),('DOWNLOAD_DATA_SIZE','Integer',4,'0'),('SYSTEM_VER','Integer',4,'0'),('TITLE','Utf8',128,'NickLabs AI on Garlic LLM'),('TITLE_ID','Utf8',12,'NLNA00001'),('VERSION','Utf8',8,'00.02')]:
 pkg('sfo_setentry',sfo,key,'--type',typ,'--maxsize',str(size),'--value',value)
files=[p.relative_to(stage).as_posix() for p in sorted(stage.rglob('*')) if p.is_file() and p.suffix!='.gp4']
run([sdk/'bin/linux/create-gp4','-out','native.gp4','--content-id='+cid,'--files',' '.join(files)])
pkg('pkg_build','native.gp4',str(out))
pkg('pkg_validate',str(out/(cid+'.pkg')))
p=out/(cid+'.pkg');sha=hashlib.sha256(p.read_bytes()).hexdigest()
print(p,p.stat().st_size,sha)
(out/'package.json').write_text(json.dumps({'title_id':'NLNA00001','content_id':cid,'version':'00.06','pkg':p.name,'size':p.stat().st_size,'sha256':sha,'eboot_sha256':hashlib.sha256((stage/'eboot.bin').read_bytes()).hexdigest()},indent=2)+'\n')
