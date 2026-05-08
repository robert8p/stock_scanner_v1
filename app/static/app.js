const scanRequestKey = `scan-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();
    const feedback = document.getElementById('scan-feedback');
    const bar = document.getElementById('scan-progress-bar');
    const label = document.getElementById('scan-progress-label');
    const build = data.build ? `${data.build.app_version} · ${data.build.build_id}` : '';
    if (feedback) {
      const latest = data.latest_run && data.latest_run.status ? ` Latest run: ${data.latest_run.status}.` : '';
      feedback.textContent = `${data.phase || 'idle'} — ${data.message || 'Ready'}.${latest}${build ? ` Build: ${build}.` : ''}`;
    }
    if (bar && label) {
      const total = data.progress_total || 0;
      const current = data.progress_current || 0;
      const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
      bar.style.width = `${pct}%`;
      label.textContent = data.is_running
        ? `${current}/${total} · ${pct}% · ${data.phase || 'running'}`
        : (data.phase === 'completed' ? 'Completed. Refresh Latest Results if needed.' : 'No active scan.');
    }
    const btn = document.getElementById('run-scan-btn');
    if (btn) btn.disabled = !!data.is_running;
  } catch (err) {
    console.error(err);
  }
}

async function runScan() {
  const btn = document.getElementById('run-scan-btn');
  const feedback = document.getElementById('scan-feedback');
  if (btn) btn.disabled = true;
  if (feedback) feedback.textContent = 'Starting scan...';
  try {
    const res = await fetch('/api/scan/run', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Idempotency-Key': scanRequestKey,
      },
      body: JSON.stringify({ request_key: scanRequestKey }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Failed to start scan');
    if (feedback) feedback.textContent = `Scan started: ${body.run_id}`;
  } catch (err) {
    if (feedback) feedback.textContent = err.message || 'Failed to start scan';
    if (btn) btn.disabled = false;
  }
  await pollStatus();
}

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('run-scan-btn');
  if (btn) btn.addEventListener('click', runScan);
  pollStatus();
  setInterval(pollStatus, 2500);
});
