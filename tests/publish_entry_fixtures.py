"""Commit one complete generation of genuine declared test artifacts.

An exclusive publisher lock refuses concurrent/foreign mutation. The final
marker records complete actual image bytes; the consumer captures that exact
unit before the original assertions. These are mutable derived outputs only.
"""
from pathlib import Path
import os, stat, json, hashlib, ctypes
SCHEMA = 'codetracer-trace-format.entry-fixtures.v1'
NAMES = {'marked.ct', 'unmarked.ct', '.producer-owner'}

def unique_keys(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise RuntimeError('Duplicate generation marker key')
        result[key]=value
    return result

def identity(path):
    path = Path(path); info = path.lstat()
    if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise RuntimeError('Reparse/link authority refused')
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), str(path.resolve(strict=True)))

def held_file(path):
    path = Path(path); before = identity(path)
    if before[2] != stat.S_IFREG: raise RuntimeError('Nonordinary artifact')
    if os.name == 'nt':
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
        kernel.CreateFileW.restype = ctypes.c_void_p
        handle = kernel.CreateFileW(str(path),0x80000000,1,None,3,0x00200000,None)
        if handle in (None,ctypes.c_void_p(-1).value): raise OSError(ctypes.get_last_error(),'Cannot hold artifact')
        fd = msvcrt.open_osfhandle(handle,os.O_RDONLY|os.O_BINARY)
    else:
        fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    file = os.fdopen(fd,'rb')
    actual = os.fstat(file.fileno())
    if (actual.st_dev,actual.st_ino,stat.S_IFMT(actual.st_mode),stat.S_IMODE(actual.st_mode)) != before[:4]:
        file.close(); raise RuntimeError('Artifact image changed on admission')
    return file,before

# Windows identity of a held handle, in os.stat's terms.
#
# Since CPython 3.12 (gh-99726) os.stat/os.lstat/os.fstat on Windows report
# st_dev as the 64-bit volume serial and st_ino as the 128-bit file id of
# FILE_ID_INFO, not the 32-bit dwVolumeSerialNumber and 64-bit nFileIndex of
# BY_HANDLE_FILE_INFORMATION. On NTFS the 64-bit serial differs from its 32-bit
# truncation, and on ReFS the 128-bit id need not fit the 64-bit index, so a
# handle's GetFileInformationByHandle values cannot be compared with st_dev /
# st_ino. The derivation below is CPython 3.13's _Py_attribute_data_to_stat
# (Python/fileutils.c), which os.fstat (_Py_fstat_noraise) and the os.lstat
# handle path (win32_xstat_slow_impl, Modules/posixmodule.c) both use:
#   st_dev = FILE_ID_INFO.VolumeSerialNumber if FileIdInfo was obtained,
#            else dwVolumeSerialNumber;
#   st_ino = FILE_ID_INFO.FileId as a little-endian 128-bit integer (the
#            id_128_to_ino union, joined by _pystat_l128_from_l64_l64), and
#            only when that is absent or zero, nFileIndexHigh<<32 | nFileIndexLow.
# The os.lstat fast path (GetFileInformationByName, FileStatBasicByNameInfo)
# reports the same 64-bit serial and 128-bit id, without any fallback. A volume
# that answers only one of the two forms therefore yields a mismatch, which
# refuses the hold rather than accepting a weaker binding.
# https://learn.microsoft.com/windows/win32/api/winbase/ns-winbase-file_id_info
# https://learn.microsoft.com/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information
FILE_ID_INFO_CLASS = 18  # FILE_INFO_BY_HANDLE_CLASS.FileIdInfo (minwinbase.h)

class ByHandleFileInformation(ctypes.Structure):
    """BY_HANDLE_FILE_INFORMATION: FILETIMEs are two DWORDs, so 4-byte packed, 52 bytes."""
    _pack_=4
    _fields_=[('attributes',ctypes.c_uint32),('creation',ctypes.c_uint64),('access',ctypes.c_uint64),('write',ctypes.c_uint64),('volume',ctypes.c_uint32),('sizeHigh',ctypes.c_uint32),('sizeLow',ctypes.c_uint32),('links',ctypes.c_uint32),('indexHigh',ctypes.c_uint32),('indexLow',ctypes.c_uint32)]

class FileIdInformation(ctypes.Structure):
    """FILE_ID_INFO: ULONGLONG VolumeSerialNumber + FILE_ID_128 (BYTE[16]), 24 bytes."""
    _fields_=[('volume',ctypes.c_uint64),('identifier',ctypes.c_ubyte*16)]

if ctypes.sizeof(ByHandleFileInformation)!=52 or ctypes.sizeof(FileIdInformation)!=24 or FileIdInformation.identifier.offset!=8:
    raise RuntimeError('Windows file identity layout mismatch')

def cpython_windows_identity(volume, index_high, index_low, id_info):
    """(st_dev, st_ino) as CPython 3.12+ derives them; id_info is
    (VolumeSerialNumber, FileId bytes) or None when FileIdInfo was not obtained."""
    dev=volume;ino=0
    if id_info is not None:
        serial,identifier=id_info
        if len(identifier)!=16:raise RuntimeError('Malformed FILE_ID_128')
        dev=serial;ino=int.from_bytes(bytes(identifier),'little')
    if not ino:ino=(index_high<<32)+index_low
    return dev,ino

def bind_windows_identity_queries(kernel):
    kernel.GetFileInformationByHandle.argtypes=[ctypes.c_void_p,ctypes.POINTER(ByHandleFileInformation)]
    kernel.GetFileInformationByHandle.restype=ctypes.c_int
    kernel.GetFileInformationByHandleEx.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32]
    kernel.GetFileInformationByHandleEx.restype=ctypes.c_int

def windows_handle_identity(kernel, handle):
    """(st_dev, st_ino, attributes) of a held Windows handle (see above)."""
    info=ByHandleFileInformation()
    if not kernel.GetFileInformationByHandle(handle,ctypes.byref(info)):raise OSError(ctypes.get_last_error(),'Held identity query failed')
    ids=FileIdInformation()
    # CPython ignores a FileIdInfo failure (pIdInfo = NULL); so does this.
    id_info=(ids.volume,bytes(ids.identifier)) if kernel.GetFileInformationByHandleEx(handle,FILE_ID_INFO_CLASS,ctypes.byref(ids),ctypes.sizeof(ids)) else None
    return (*cpython_windows_identity(info.volume,info.indexHigh,info.indexLow,id_info),info.attributes)

class HeldDirectory:
    def __init__(self,path):
        self.path=Path(path);self.before=identity(self.path)
        if self.before[2]!=stat.S_IFDIR: raise RuntimeError('Nonordinary directory')
        if os.name=='nt':
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
            self.kernel.CreateFileW.restype=ctypes.c_void_p
            self.kernel.CloseHandle.argtypes=[ctypes.c_void_p]
            # Read attributes; share read/write but NOT deletion of this parent.
            self.handle=self.kernel.CreateFileW(str(self.path),0x80,3,None,3,0x02200000,None)
            if self.handle in (None,ctypes.c_void_p(-1).value): raise OSError(ctypes.get_last_error(),'Cannot hold output parent')
            bind_windows_identity_queries(self.kernel)
            try:self.native=self.handle_identity()
            except BaseException:self.close();raise
            # Bind the held handle to the admitted object: its (st_dev, st_ino),
            # derived from the handle exactly as os.lstat derived them from the
            # path, must be the admitted ones.
            if self.native[:2]!=self.before[:2]:
                self.close();raise RuntimeError('Changed held Windows parent')
        else:
            self.fd=os.open(self.path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            actual=os.fstat(self.fd)
            if (actual.st_dev,actual.st_ino,stat.S_IFMT(actual.st_mode),stat.S_IMODE(actual.st_mode))!=self.before[:4]:
                self.close();raise RuntimeError('Changed held POSIX parent')
        self.check()
    def handle_identity(self):
        dev,ino,attributes=windows_handle_identity(self.kernel,self.handle)
        if attributes&0x400 or not attributes&0x10:raise RuntimeError('Foreign held native parent')
        return (dev,ino,attributes)
    def check(self):
        if identity(self.path)!=self.before:raise RuntimeError('Changed lexical/resolved parent authority')
        if os.name=='nt':
            if self.handle_identity()!=self.native:raise RuntimeError('Changed held native directory')
        else:
            info=os.fstat(self.fd)
            if (info.st_dev,info.st_ino,stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode))!=self.before[:4]:raise RuntimeError('Changed held directory')
    def close(self):
        if os.name=='nt':self.kernel.CloseHandle(self.handle)
        else:os.close(self.fd)

def listing(directory, fd):
    """The current members of the held directory.

    Listing through the held descriptor itself (os.listdir(fd)) reads with the
    descriptor's own directory stream, and on btrfs that stream keeps returning
    the membership from its first read, so entries created since are missing
    (measured on Linux 7.2 / btrfs, Python 3.13). A fresh descriptor opened
    relative to the held one names the same directory, with no path lookup to
    race, and lists it as it is now."""
    if fd is None:
        return os.listdir(directory)
    fresh = os.open('.', os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
    try:
        return os.listdir(fresh)
    finally:
        os.close(fresh)

def publish(root,source,destination):
    root=Path(root).resolve(strict=True);destination=Path(destination).absolute()
    if destination!=root/'target'/'entry-fixtures':raise RuntimeError('Foreign output namespace')
    parents=[];images=[];lock=None
    try:
        for parent in [root,root/'target']:parents.append(HeldDirectory(parent))
        if os.name=='nt':destination.mkdir(mode=0o700,exist_ok=True)
        else:
            try:os.mkdir('entry-fixtures',mode=0o700,dir_fd=parents[-1].fd)
            except FileExistsError:pass
        parents.append(HeldDirectory(destination))
        destination_fd=None if os.name=='nt' else parents[-1].fd
        lock=destination/'.publisher-lock'
        lock_fd=os.open(lock if os.name=='nt' else '.publisher-lock',os.O_WRONLY|os.O_CREAT|os.O_EXCL| (0 if os.name=='nt' else os.O_NOFOLLOW),0o600,**({} if os.name=='nt' else {'dir_fd':destination_fd}))
        with os.fdopen(lock_fd,'wb') as stream:stream.write(SCHEMA.encode()+b'\n');stream.flush();os.fsync(stream.fileno())
        lock_identity=identity(lock)
        members=set(listing(destination,destination_fd))-{'.publisher-lock'}
        if members and members!=NAMES:raise RuntimeError('Incomplete/foreign previous output unit')
        if members:
            old={}
            for name in NAMES:
                file,before=held_file(destination/name);body=file.read();file.close();old[name]=body
                if identity(destination/name)!=before:raise RuntimeError('Changed previous output unit')
            marker=json.loads(old['.producer-owner'],object_pairs_hook=unique_keys)
            if set(marker)!={'schema','images'} or marker.get('schema')!=SCHEMA or set(marker.get('images',{}))!={'marked.ct','unmarked.ct'}:raise RuntimeError('Foreign generation marker')
            for name in ['marked.ct','unmarked.ct']:
                if marker['images'][name]!=old[name].hex():raise RuntimeError('Previous generation correspondence refused')
        payloads={}
        for name in ['marked.ct','unmarked.ct']:
            file,before=held_file(Path(source)/name);body=file.read();file.seek(0)
            if file.read()!=body or identity(Path(source)/name)!=before:raise RuntimeError('Changed producer artifact')
            images.append((file,Path(source)/name,before,body));payloads[name]=body
        payloads['.producer-owner']=(json.dumps({'schema':SCHEMA,'images':{name:payloads[name].hex() for name in ['marked.ct','unmarked.ct']}},sort_keys=True)+'\n').encode()
        for name,body in payloads.items():
            for parent in parents:parent.check()
            if identity(lock)!=lock_identity:raise RuntimeError('Changed exclusive publisher lock')
            temporary=destination/('.entry-output-'+os.urandom(12).hex())
            fd=os.open(temporary if os.name=='nt' else temporary.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL| (0 if os.name=='nt' else os.O_NOFOLLOW),0o600,**({} if os.name=='nt' else {'dir_fd':destination_fd}))
            with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
            for parent in parents:parent.check()
            if os.name=='nt':os.replace(temporary,destination/name)
            else:os.replace(temporary.name,name,src_dir_fd=destination_fd,dst_dir_fd=destination_fd)
        if set(listing(destination,destination_fd))!=NAMES|{'.publisher-lock'}:raise RuntimeError('Changed final output membership')
        for name,body in payloads.items():
            file,before=held_file(destination/name)
            with file:
                if file.read()!=body or identity(destination/name)!=before:raise RuntimeError('Changed committed output bytes')
        for file,path,before,body in images:
            file.seek(0)
            if file.read()!=body or identity(path)!=before:raise RuntimeError('Producer changed during publication')
        for parent in parents:parent.check()
        if identity(lock)!=lock_identity:raise RuntimeError('Foreign publisher lock on release')
        if os.name=='nt':lock.unlink()
        else:os.unlink('.publisher-lock',dir_fd=destination_fd)
        lock=None
    finally:
        for file,*_ in images:file.close()
        for parent in reversed(parents):parent.close()
        # Failure keeps the lock/partial output evidence; no unknown cleanup.
