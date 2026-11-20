// SPDX-License-Identifier: GPL-3.0-or-later
#include "native_stat.hpp"
#ifdef __ORBIS__
extern "C" int _fstat(int,void*);
static_assert(sizeof(mode_t)==4 && offsetof(struct stat,st_uid)==16 && offsetof(struct stat,st_size)==80,"Pinned SDK stat ABI changed: review adapter");
extern "C" int fstat(int fd,struct stat* out){
 return native_stat::fetch(fd,out,[](int n,native_stat::Wire* w){return _fstat(n,w);});
}
#endif
