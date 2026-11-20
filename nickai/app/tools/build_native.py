#!/usr/bin/env python3
"""Linux OpenOrbis compile/link of NickLabs AI on Garlic LLM (title NLNA00001). Never connects to a console.
Reuses the toolchain/OpenSSL built for the worker (read-only); all outputs go to native-app/build/."""
from pathlib import Path
import os,subprocess,sys
a=Path(__file__).resolve().parents[1];root=a.parent;w=Path(os.environ.get('NLAG_BUILD_ROOT',root/'ps4-worker'))   # folder with build/OpenOrbis, build/openssl-3.5.4 and vendor/json.hpp (see BUILDING.md)
sdk=Path(os.environ.get('OO_PS4_TOOLCHAIN',w/'build/OpenOrbis/PS4Toolchain'))
out=a/'build/native';out.mkdir(parents=True,exist_ok=True)
ssl=w/'build/openssl-3.5.4'
def run(args,cwd=None):
 print(' '.join(map(str,args)),flush=True);subprocess.run(list(map(str,args)),check=True,cwd=cwd,env=dict(os.environ,OO_PS4_TOOLCHAIN=str(sdk)))
run(['python3',a/'tools/prepare_engine.py'])
base=['-ffile-prefix-map='+str(root)+'=/src','-ffile-prefix-map='+str(w)+'=/build','-fdebug-prefix-map='+str(root)+'=/src','-target','x86_64-freebsd-none','-march=btver2','-O2','-fPIC','-funwind-tables','-fno-stack-protector','-fshort-wchar','-Wno-deprecated-declarations','-D__ORBIS__','-D_GNU_SOURCE','-D_BSD_SOURCE','-D_DEFAULT_SOURCE','-D_POSIX_C_SOURCE=200809L','-nostdinc','-isystem',sdk/'include','-I',a/'build/engine']
objs=[]
for src in [a/'build/engine/engine.c',a/'build/engine/tokenizer.c',a/'src/main.cpp',a/'src/garlic_backend.cpp',a/'src/native_stat.cpp']:
 obj=out/(src.stem+'.o');objs.append(obj)
 flags=['-std=c++17','-nostdinc++','-isystem',sdk/'include/c++/v1','-I',w/'vendor','-I',a/'src','-I',ssl/'include'] if src.suffix=='.cpp' else ['-std=gnu11']
 run(['clang++' if src.suffix=='.cpp' else 'clang',*base,*flags,'-c',src,'-o',obj])
run(['ld.lld','-m','elf_x86_64','-pie','--script',sdk/'link.x','--eh-frame-hdr',*objs,ssl/'libcrypto.a','-L',sdk/'lib','-lc++','-lc','-lm','-lkernel','-lSceGnmDriver','-lSceNet','-lSceNetCtl','-lSceSysmodule','-lSceSystemService',sdk/'lib/crt1.o','-o',out/'native.elf'])
run([sdk/'bin/linux/create-fself','-in=native.elf','-out=native.oelf','--eboot=eboot.bin','--paid','0x3800000000000011'],cwd=out)   # relative names: the build folder's path (and so the user name in it) is not written into the file
print('built',out/'eboot.bin',(out/'eboot.bin').stat().st_size)
