/* Shared page gate: other open clients enter the same server-enforced takeover. */
(() => {
  const progressPage = document.body.hasAttribute('data-upgrade-page');
  const message = document.getElementById('upgrade-message');
  const resume = document.getElementById('upgrade-resume');
  let pending = false;
  async function poll() {
    if (pending) return;
    pending = true;
    try {
      const response = await fetch('/api/settings/data-upgrade', { cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const state = await response.json();
      if (progressPage) {
        if (!state.blocked) { window.location.replace('/'); return; }
        const running = state.status === 'running';
        const checking = state.action === 'check';
        const repairing = state.action === 'repair';
        const backingUp = state.action === 'backup';
        document.getElementById('upgrade-title').textContent = backingUp ? (running ? '正在备份数据库' : '备份需要继续') : running ? (repairing ? '正在修复缺失原文件' : (checking ? '正在检查数据' : '正在升级数据')) : (repairing ? '修复需要继续' : (checking ? '检查需要继续' : '升级需要继续'));
        resume.textContent = backingUp ? '继续备份' : repairing ? '继续修复' : (checking ? '继续检查' : '继续升级');
        message.textContent = state.message;
        document.getElementById('upgrade-progress').value = state.progress || 0;
        document.getElementById('upgrade-percent').textContent = `${state.progress || 0}%`;
        document.getElementById('upgrade-current').textContent = state.current || '';
        document.getElementById('upgrade-error').textContent = state.error || '';
        resume.hidden = running;
      } else if (state.blocked) {
        window.location.reload();
      }
    } catch (error) {
      if (message) message.textContent = '暂时无法读取进度，正在重新连接…';
    } finally {
      pending = false;
      window.setTimeout(poll, progressPage ? 1000 : 5000);
    }
  }
  if (resume) resume.addEventListener('click', async () => {
    resume.disabled = true;
    try {
      const response = await fetch('/api/settings/data-upgrade/start', { method: 'POST' });
      if (!response.ok) throw new Error((await response.json()).detail || '无法继续升级');
      resume.hidden = true;
    } catch (error) { document.getElementById('upgrade-error').textContent = error.message; }
    finally { resume.disabled = false; }
  });
  poll();
})();
