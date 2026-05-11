const scanRequestKey = `scan-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
const replayRequestKey = `replay-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

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

async function pollReplayStatus() {
  try {
    const res = await fetch('/api/replay/status');
    if (!res.ok) return;
    const data = await res.json();
    const feedback = document.getElementById('replay-feedback');
    const bar = document.getElementById('replay-progress-bar');
    const label = document.getElementById('replay-progress-label');
    const build = data.build ? `${data.build.app_version} · ${data.build.build_id}` : '';
    if (feedback) {
      const latest = data.latest_replay && data.latest_replay.status ? ` Latest replay: ${data.latest_replay.status}.` : '';
      feedback.textContent = `${data.phase || 'idle'} — ${data.message || 'Ready'}.${latest}${build ? ` Build: ${build}.` : ''}`;
    }
    if (bar && label) {
      const total = data.progress_total || 0;
      const current = data.progress_current || 0;
      const pct = total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0;
      bar.style.width = `${pct}%`;
      label.textContent = data.is_running
        ? `${current}/${total} · ${pct}% · ${data.phase || 'running'}`
        : (data.phase === 'completed' ? 'Completed. Refresh Replay / Calibration if needed.' : 'No active replay.');
    }
    const btn = document.getElementById('run-replay-btn');
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

async function runReplay() {
  const btn = document.getElementById('run-replay-btn');
  const feedback = document.getElementById('replay-feedback');
  if (btn) btn.disabled = true;
  if (feedback) feedback.textContent = 'Starting replay...';
  try {
    const res = await fetch('/api/replay/run', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Idempotency-Key': replayRequestKey,
      },
      body: JSON.stringify({ request_key: replayRequestKey }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || 'Failed to start replay');
    if (feedback) feedback.textContent = `Replay started: ${body.replay_id}`;
  } catch (err) {
    if (feedback) feedback.textContent = err.message || 'Failed to start replay';
    if (btn) btn.disabled = false;
  }
  await pollReplayStatus();
}

document.addEventListener('DOMContentLoaded', () => {
  const scanBtn = document.getElementById('run-scan-btn');
  if (scanBtn) scanBtn.addEventListener('click', runScan);
  const replayBtn = document.getElementById('run-replay-btn');
  if (replayBtn) replayBtn.addEventListener('click', runReplay);
  pollStatus();
  pollReplayStatus();
  setInterval(pollStatus, 2500);
  setInterval(pollReplayStatus, 3000);
});
