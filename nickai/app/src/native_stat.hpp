// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <cstdint>
#include <cstddef>
#include <sys/stat.h>
// PS4's FreeBSD9 kernel ABI. The pinned OpenOrbis release asset has a 32-bit
// mode_t in its public struct stat, moving uid/size/timestamps by 4/8 bytes.
// Keep the SDK ABI intact for prebuilt libc++ and translate at the boundary.
namespace native_stat {
struct Time {int64_t sec,nsec;};
struct Wire {
 uint32_t dev,ino;
 uint16_t mode,nlink;
 uint32_t uid,gid,rdev;
 Time atime,mtime,ctime;
 int64_t size,blocks;
 uint32_t blksize,flags,gen,spare;
 Time birth;
};
static_assert(sizeof(Wire)==120,"PS4 stat ABI size");
static_assert(offsetof(Wire,mode)==8 && offsetof(Wire,nlink)==10 && offsetof(Wire,uid)==12 && offsetof(Wire,gid)==16,"PS4 ownership ABI");
static_assert(offsetof(Wire,atime)==24 && offsetof(Wire,size)==72 && offsetof(Wire,blocks)==80 && offsetof(Wire,birth)==104,"PS4 size/time ABI");
inline void convert(const Wire& w,struct stat& s){
 s={};s.st_dev=w.dev;s.st_ino=w.ino;s.st_mode=w.mode;s.st_nlink=w.nlink;
 s.st_uid=w.uid;s.st_gid=w.gid;s.st_rdev=w.rdev;
 s.st_atim.tv_sec=w.atime.sec;s.st_atim.tv_nsec=w.atime.nsec;
 s.st_mtim.tv_sec=w.mtime.sec;s.st_mtim.tv_nsec=w.mtime.nsec;
 s.st_ctim.tv_sec=w.ctime.sec;s.st_ctim.tv_nsec=w.ctime.nsec;
 s.st_size=w.size;s.st_blocks=w.blocks;s.st_blksize=w.blksize;
#ifdef __ORBIS__
 s.st_flags=w.flags;s.st_gen=w.gen;s.st_birthtim.tv_sec=w.birth.sec;s.st_birthtim.tv_nsec=w.birth.nsec;
#endif
}
template<class Call> inline int fetch(int fd,struct stat* out,Call call){
 Wire wire{};int rc=call(fd,&wire);
 // _fstat is the POSIX libkernel export already used by SDK libc. Keep its
 // return and errno unchanged; do not decode Sony sceKernel negative errors.
 if(rc)return rc;
 convert(wire,*out);return 0;
}
}
