/* The Google session authenticates UI calls; no worker secret reaches the browser. */
(() => {
  const pending = new Map();
  let cachedState = null;
  function rpc(path, payload, requestId) {
    return new Promise((resolve, reject) => {
      google.script.run.withSuccessHandler(result => {
        if (result.ok) resolve(result.value);
        else reject(new Error(result.error || '요청 실패'));
      }).withFailureHandler(() => reject(new Error('클라우드 연결을 확인해 주세요. 요청한 작업은 수집 기록에서 확인할 수 있습니다.')))
        .uiRequest(path, payload || {}, requestId || '');
    });
  }
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  window.bookingCloudApi = async (path, payload) => {
    if (path === '/api/jobs') {
      const result = await rpc(path);
      if (cachedState) cachedState.jobs = result.jobs;
      return result;
    }
    if (path === '/api/state') {
      const next = await rpc(path, {manifest: cachedState?.manifest, jobs_key: cachedState?.jobs_key});
      const jobs = next.jobs || cachedState?.jobs || [];
      cachedState = next.not_modified ? {...cachedState, ...next} : next;
      cachedState.jobs = jobs.map(job => job.id === next.job?.id ? {...job, ...next.job} : job);
      return cachedState;
    }
    if (payload === undefined || path === '/api/stop' || path === '/api/job') return rpc(path, payload);
    const signature = JSON.stringify([path, payload]);
    if (pending.has(signature)) return pending.get(signature);
    const task = (async () => {
      const result = await rpc(path, payload, crypto.randomUUID());
      if (path === '/api/start' || (['/api/apply', '/api/approve-review'].includes(path) && payload.collect_after && !payload.wait_for_completion)) return result;
      const banner = document.getElementById('cloudPending');
      banner.hidden = false;
      banner.textContent = '변경사항 저장 대기 중';
      try {
        for (let n = 0; n < 180; n++) {
          await pause(10000);
          const job = await rpc('/api/job', {id: result.job_id});
          banner.textContent = job.message;
          if (payload.wait_for_completion && ['completed', 'issues'].includes(job.state)) {
            if (!job.response?.keys?.length) throw new Error('저장 결과를 확인할 수 없습니다. 수집 기록을 확인해 주세요.');
            return {...job.response, collection_state: job.state};
          }
          if (job.state === 'completed') return job.response;
          if (['failed', 'interrupted', 'stopped'].includes(job.state)) throw new Error(job.message);
        }
        throw new Error('작업 확인 대기시간이 지났습니다. 수집 기록에서 상태를 확인해 주세요.');
      } finally { banner.hidden = true; }
    })();
    pending.set(signature, task);
    try { return await task; } finally { pending.delete(signature); }
  };
})();
