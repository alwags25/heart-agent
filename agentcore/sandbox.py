"""受限令牌降权(尽力而为的增强围栏):
Windows 下把扩展/代码子进程的当前线程 impersonate 为受限令牌 —— 禁用全部特权、
把 Administrators 组设为 deny-only,降低"误删系统文件 / 改注册表 / 关服务"这类
越界操作的能力。

诚实声明:这是增强围栏,不是完整沙箱;失败静默回退,不影响正常执行。
默认关闭,由环境变量 HEARTAGENT_RESTRICTED=1 开启(config.security.restricted_token)。
"""
import os


def _admins_sid():
    """构造 Administrators 组的 SID(S-1-5-32-544),避免依赖平台导出符号。"""
    import ctypes

    class SID(ctypes.Structure):
        _fields_ = [("Revision", ctypes.c_ubyte),
                    ("SubAuthorityCount", ctypes.c_ubyte),
                    ("IdentifierAuthority", ctypes.c_ubyte * 6),
                    ("SubAuthority", ctypes.c_uint32 * 2)]

    sid = SID()
    sid.Revision = 1
    sid.SubAuthorityCount = 2
    sid.IdentifierAuthority[5] = 5  # SECURITY_NT_AUTHORITY
    sid.SubAuthority[0] = 32        # SECURITY_BUILTIN_DOMAIN_RID
    sid.SubAuthority[1] = 544       # DOMAIN_ALIAS_RID_ADMINS
    return sid


def drop_privileges():
    """把当前线程 impersonate 为受限令牌(禁特权 + 管理员组 deny-only)。返回是否成功。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes as wt

        advapi = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

        TOKEN_ALL_ACCESS = 0x000F01FF
        DISABLE_MAX_PRIVILEGE = 0x1
        SE_GROUP_USE_FOR_DENY_ONLY = 0x10
        SECURITY_IMPERSONATION = 2
        TOKEN_IMPERSONATION = 2

        advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi.OpenProcessToken.restype = ctypes.c_bool
        advapi.CreateRestrictedToken.argtypes = [
            wt.HANDLE, wt.DWORD, wt.DWORD, ctypes.POINTER(SID_AND_ATTRIBUTES),
            wt.DWORD, ctypes.POINTER(SID_AND_ATTRIBUTES), wt.DWORD,
            ctypes.POINTER(SID_AND_ATTRIBUTES), ctypes.POINTER(wt.HANDLE)]
        advapi.CreateRestrictedToken.restype = ctypes.c_bool
        advapi.DuplicateTokenEx.argtypes = [
            wt.HANDLE, wt.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(wt.HANDLE)]
        advapi.DuplicateTokenEx.restype = ctypes.c_bool
        advapi.ImpersonateLoggedOnUser.argtypes = [wt.HANDLE]
        advapi.ImpersonateLoggedOnUser.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        kernel32.GetCurrentProcess.restype = wt.HANDLE

        h_token = wt.HANDLE()
        if not advapi.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ALL_ACCESS,
                                       ctypes.byref(h_token)):
            return False

        admins = _admins_sid()
        deny_sids = (SID_AND_ATTRIBUTES * 1)(
            SID_AND_ATTRIBUTES(ctypes.cast(ctypes.byref(admins), ctypes.c_void_p),
                               SE_GROUP_USE_FOR_DENY_ONLY))

        restricted = wt.HANDLE()
        ok = advapi.CreateRestrictedToken(
            h_token, DISABLE_MAX_PRIVILEGE, 1, deny_sids, 0, None, 0, None,
            ctypes.byref(restricted))
        kernel32.CloseHandle(h_token)
        if not ok:
            return False

        # primary → impersonation token,再让当前线程 impersonate
        imp = wt.HANDLE()
        ok = advapi.DuplicateTokenEx(restricted, TOKEN_ALL_ACCESS, None,
                                     SECURITY_IMPERSONATION, TOKEN_IMPERSONATION,
                                     ctypes.byref(imp))
        kernel32.CloseHandle(restricted)
        if not ok:
            return False

        applied = bool(advapi.ImpersonateLoggedOnUser(imp))
        kernel32.CloseHandle(imp)
        return applied
    except Exception:
        return False
