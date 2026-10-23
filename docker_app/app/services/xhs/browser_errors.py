from __future__ import annotations


BROWSER_ERRORS = {
    'BROWSER_SANDBOX_BLOCKED': ('容器或系统阻止了 Chromium 沙箱启动', '请保留原数据挂载，使用本版本默认 Compose 重建主容器；仅替换镜像不会更新容器权限。'),
    'BROWSER_DISPLAY_UNAVAILABLE': ('内置浏览器显示服务未就绪', '请重启主容器；如果仍失败，检查容器临时目录是否可写。'),
    'BROWSER_NOT_INSTALLED': ('当前镜像缺少 Chromium 浏览器文件', '请重新导入完整的本版本主应用镜像并重建主容器。'),
    'BROWSER_MISSING_DEPENDENCIES': ('当前镜像缺少浏览器运行库', '请使用完整的本版本主应用镜像，不要只替换应用代码。'),
    'BROWSER_UNSUPPORTED_CPU': ('当前处理器无法运行内置 Chromium', '请确认 NAS 的处理器架构与镜像一致，并检查处理器指令集兼容性。'),
    'BROWSER_OUT_OF_MEMORY': ('启动浏览器时内存不足', '请增加主容器内存上限，并使用默认配置中的共享内存设置。'),
    'PROFILE_BUSY': ('浏览器配置目录被其他进程占用', '请停止使用相同配置目录的旧应用实例后重试，不要删除登录数据。'),
    'BROWSER_UNAVAILABLE': ('内置 Chromium 启动失败', '请使用本版本默认 Compose 重建主容器；如仍失败，请查看页面的浏览器启动诊断。'),
}


def launch_diagnostic(error) -> dict:
    # Match known startup failures; never serialize the exception, paths, arguments or browser output.
    message = str(error).lower()
    groups = [
        ('BROWSER_NOT_INSTALLED', ("executable doesn't exist", 'executable does not exist', 'browser executable not found')),
        ('BROWSER_MISSING_DEPENDENCIES', ('error while loading shared libraries', 'missing dependencies', 'host system is missing dependencies', 'cannot open shared object file')),
        ('BROWSER_UNSUPPORTED_CPU', ('sigill', 'illegal instruction', 'unsupported cpu')),
        ('PROFILE_BUSY', ('processsingleton', 'singletonlock', 'profile in use', 'user data directory is already in use')),
        ('BROWSER_DISPLAY_UNAVAILABLE', ('missing x server', 'cannot open display', 'unable to open x display', 'without having a xserver', 'failed to connect to the bus: failed to connect to socket /tmp/.x11-unix')),
        ('BROWSER_SANDBOX_BLOCKED', ('no usable sandbox', 'failed to move to new namespace', 'failed to create a user namespace', 'user namespace is not supported', 'namespace sandbox', 'sys_chroot', 'zygote_host_impl_linux.cc', 'unshare failed')),
        ('BROWSER_OUT_OF_MEMORY', ('out of memory', 'cannot allocate memory', 'oom-kill')),
    ]
    category = next((key for key, patterns in groups if any(pattern in message for pattern in patterns)), 'BROWSER_UNAVAILABLE')
    title, action = BROWSER_ERRORS[category]
    return {'category': category, 'message': title, 'action': action, 'sandbox_enabled': True}


def browser_error_text(category: str) -> str:
    if category in BROWSER_ERRORS:
        title, action = BROWSER_ERRORS[category]
        return title + '。' + action
    auth_errors = {
        'LOGIN_IN_PROGRESS': '尚未确认登录成功，请在手机上确认扫码登录，稍候再试。二维码会继续检测。',
        'LOGIN_REQUIRED': '扫码会话已结束，请重新启动扫码登录。',
        'NEEDS_REAUTH': '小红书登录状态尚未确认，请重新扫码并在手机上确认。',
        'NEEDS_REBIND': '当前扫码账号与已绑定账号不一致，请使用原账号登录。',
        'NEEDS_MANUAL_VERIFICATION': '小红书要求安全验证，请打开验证画面完成操作后继续登录。',
        'LOGIN_FRAME_EXPIRED': '画面已更新，请等待刷新后重新操作。',
        'LOGIN_INPUT_INVALID': '操作无效或拖动时间过长，请重试。',
        'RATE_LIMITED': '小红书暂时限制请求，请稍后再扫码登录。',
    }
    if category in auth_errors:
        return auth_errors[category]
    return '小红书浏览器状态：' + category
