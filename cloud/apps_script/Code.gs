/* Owner-only UI and HMAC-authenticated GitHub worker bridge. */
const ROLES = ['db.xlsx', 'result.xlsx', 'catalog.json', 'workspace.json', 'state.json', 'history.json'];
const TERMINAL = ['completed', 'issues', 'stopped', 'failed', 'interrupted'];
const UUID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const MAX_ZIP = 8000000;
const MAX_EXPANDED = 24000000;

function props_() { return PropertiesService.getScriptProperties(); }
function setting_(name) {
  const value = props_().getProperty(name);
  if (!value) throw new Error('설정이 필요합니다: ' + name);
  return value;
}
function owner_() {
  const email = Session.getActiveUser().getEmail().toLowerCase();
  if (!email || email !== setting_('OWNER_EMAIL').toLowerCase()) throw new Error('소유자 계정으로 로그인해 주세요.');
}
function bridgeError_(code, message, retryable) {
  const error = new Error(message);
  error.bridgeCode = code;
  error.retryable = retryable === true;
  return error;
}
function locked_(fn) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(15000)) throw bridgeError_('BUSY', '다른 요청 처리 중입니다. 잠시 후 자동으로 다시 연결합니다.', true);
  try { return fn(); } finally { lock.releaseLock(); }
}
function json_(id) { return JSON.parse(DriveApp.getFileById(id).getBlob().getDataAsString('UTF-8')); }
function saveJson_(folder, name, value) { return folder.createFile(name, JSON.stringify(value), 'application/json').getId(); }
function hex_(bytes) { return bytes.map(b => ('0' + ((b + 256) % 256).toString(16)).slice(-2)).join(''); }
function sha_(bytes) { return hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes)); }
function same_(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
function iso_() { return new Date(Date.now() + 9 * 3600000).toISOString().replace('Z', '+09:00'); }
function root_() { return DriveApp.getFolderById(setting_('ROOT_FOLDER_ID')); }
function jobsFolder_() { return DriveApp.getFolderById(setting_('JOBS_FOLDER_ID')); }
function manifest_(id) {
  const cache = CacheService.getScriptCache(), key = 'manifest:' + id;
  const cached = cache.get(key);
  if (cached) return JSON.parse(cached);
  const value = json_(id), text = JSON.stringify(value);
  if (text.length < 20000) cache.put(key, text, 21600);
  return value;
}
function current_() { return manifest_(setting_('CURRENT_MANIFEST_ID')); }
function fileUrl_(id) { return 'https://drive.google.com/file/d/' + id + '/view'; }

function findJob_(id) {
  if (!UUID.test(id || '')) throw new Error('잘못된 작업번호');
  const files = jobsFolder_().getFilesByName(id + '.json');
  if (!files.hasNext()) return null;
  const file = files.next();
  if (files.hasNext()) throw new Error('중복 작업 기록. 관리자 확인 필요');
  const job = JSON.parse(file.getBlob().getDataAsString('UTF-8'));
  job._fileId = file.getId();
  return job;
}
function saveJob_(job) {
  const value = Object.assign({}, job);
  delete value._fileId;
  if (job._fileId) DriveApp.getFileById(job._fileId).setContent(JSON.stringify(value));
  else job._fileId = saveJson_(jobsFolder_(), job.id + '.json', value);
  CacheService.getScriptCache().remove('job-summary:' + job.id);
}
function summaryJob_(id) {
  const cache = CacheService.getScriptCache();
  const cached = cache.get('job-summary:' + id);
  if (cached) return JSON.parse(cached);
  const job = publicJob_(findJob_(id));
  delete job.logs;
  delete job.response;
  job.remote_log = true;
  if (TERMINAL.includes(job.state)) cache.put('job-summary:' + id, JSON.stringify(job), 300);
  return job;
}
function publicJob_(job) {
  if (!job) return {state: 'idle', message: '대기 중', total: 0, completed: 0, logs: []};
  return {id: job.id, kind: job.kind, state: job.state, date: job.payload.date,
    started_at: job.started_at, finished_at: job.finished_at || null,
    message: job.message, total: job.total || 0, completed: job.completed || 0,
    normal: job.normal || 0, issues: job.issues || 0, logs: job.logs || [],
    response: job.response || null};
}
function recoverJob_(job) {
  if (!job || TERMINAL.includes(job.state)) return job;
  const manifest = current_();
  // Recover a crash after publishing the snapshot but before recording completion.
  if (manifest.job_id === job.id) {
    progress_(job, manifest.outcome);
    Object.assign(job, {state: manifest.outcome.state, response: manifest.response, finished_at: manifest.created_at});
    saveJob_(job);
    return job;
  }
  const age = Date.now() - Date.parse(job.heartbeat || job.started_at);
  if (age > (job.state === 'queued' ? 25 * 60 * 1000 : 5 * 60 * 1000)) {
    Object.assign(job, {state: 'interrupted', message: '클라우드 작업 응답이 끊겼습니다. 마지막 저장 결과는 유지됩니다.', finished_at: iso_()});
    saveJob_(job);
  }
  return job;
}
function active_() {
  const id = props_().getProperty('ACTIVE_JOB_ID');
  const job = id ? recoverJob_(findJob_(id)) : null;
  if (job && TERMINAL.includes(job.state)) props_().deleteProperty('ACTIVE_JOB_ID');
  return job;
}
function compactState_(state) {
  // Raw page text remains in the private snapshot; the editor needs labels only.
  (state.entries || []).forEach(entry => {
    Object.keys(entry.item_groups || {}).forEach(group => {
      entry.item_groups[group] = entry.item_groups[group].map(item => {
        const small = {};
        ['itemKey', 'title', 'decision', 'reason', 'mode'].forEach(k => small[k] = item[k]);
        return small;
      });
    });
  });
  return state;
}
function state_(knownManifest, knownJobs) {
  const snapshot = locked_(() => ({job: active_(), manifestId: setting_('CURRENT_MANIFEST_ID'),
    ids: JSON.parse(props_().getProperty('RECENT_JOB_IDS') || '[]')}));
  const {job, manifestId, ids} = snapshot;
  // Immutable snapshot files and old job summaries do not need the worker lock.
  const manifest = manifest_(manifestId);
  const state = knownManifest === manifestId ? {not_modified: true} : compactState_(json_(manifest.files['state.json']));
  state.manifest = manifestId;
  state.jobs_key = JSON.stringify(ids);
  if (knownJobs !== state.jobs_key) state.jobs = ids.slice(0, 1).map(id => summaryJob_(id));
  else delete state.jobs;
  state.job = job ? publicJob_(job) : (ids.length ? summaryJob_(ids[0]) : publicJob_(null));
  delete state.job.logs;
  delete state.job.response;
  state.job.remote_log = !!state.job.id;
  state.busy = !!job && !TERMINAL.includes(job.state);
  state.cloud = true;
  state.version = '2.5.0';
  state.folder = 'Google Drive';
  state.today = Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyy-MM-dd');
  state.drive_url = 'https://drive.google.com/drive/folders/' + setting_('ROOT_FOLDER_ID');
  state.downloads = {db: fileUrl_(manifest.files['db.xlsx']), result: fileUrl_(manifest.files['result.xlsx'])};
  return state;
}

function doGet() {
  try {
    owner_();
    return HtmlService.createHtmlOutputFromFile('Index').setTitle('캠핑 인사이트 | 클라우드')
      .addMetaTag('viewport', 'width=device-width, initial-scale=1');
  } catch (_) { return HtmlService.createHtmlOutput('소유자 Google 계정으로 로그인해 주세요.'); }
}

function uiRequest(path, payload, requestId) {
  try {
    owner_();
    if (path === '/api/state') return {ok: true, value: state_(payload && payload.manifest, payload && payload.jobs_key)};
    if (path === '/api/jobs') {
      const ids = JSON.parse(props_().getProperty('RECENT_JOB_IDS') || '[]');
      return {ok: true, value: {jobs: ids.map(id => summaryJob_(id))}};
    }
    if (path.startsWith('/api/history?key=')) {
      const key = decodeURIComponent(path.split('=')[1]);
      if (!/^[a-f0-9]{20}$/.test(key)) throw new Error('잘못된 항목번호');
      return {ok: true, value: {history: json_(current_().files['history.json'])[key] || []}};
    }
    const value = locked_(() => {
      if (path === '/api/job') return publicJob_(recoverJob_(findJob_(payload.id)));
      if (path === '/api/stop') {
        const job = active_();
        if (!job || TERMINAL.includes(job.state)) return {message: '진행 중인 작업이 없습니다.'};
        job.stopRequested = true;
        job.message = '중지 요청됨. 현재 항목 처리 후 저장합니다.';
        if (job.state === 'queued') Object.assign(job, {state: 'stopped', finished_at: iso_()});
        saveJob_(job);
        return {message: job.message};
      }
      const routes = {'/api/start': payload && payload.kind, '/api/apply': 'apply', '/api/archive': 'archive', '/api/room-rules': 'room-rules', '/api/approve-review': 'approve-review'};
      const kind = routes[path];
      if (!['collect', 'scan', 'discover', 'verify', 'apply', 'archive', 'room-rules', 'approve-review'].includes(kind)) throw new Error('알 수 없는 요청');
      return dispatch_(kind, payload, requestId);
    });
    return {ok: true, value: value};
  } catch (error) { return {ok: false, error: String(error.message).slice(0, 600)}; }
}

function dispatch_(kind, payload, requestId) {
  if (!UUID.test(requestId || '')) throw new Error('새 화면에서 다시 요청해 주세요.');
  if (!payload || typeof payload !== 'object' || JSON.stringify(payload).length > 1500000) throw new Error('요청 크기가 너무 큽니다.');
  const existing = findJob_(requestId);
  if (existing) {
    if (existing.kind !== kind || JSON.stringify(existing.payload) !== JSON.stringify(payload)) throw new Error('중복 요청 내용이 다릅니다.');
    if (existing.state === 'failed') throw new Error(existing.message);
    return {job_id: existing.id, queued: true};
  }
  const active = active_();
  if (active && !TERMINAL.includes(active.state)) throw new Error('다른 작업이 진행 중입니다.');
  const manifest = current_();
  const state = json_(manifest.files['state.json']);
  const manualForce = kind === 'apply' && payload.force_manual === true && Array.isArray(payload.edits)
    && payload.edits.length > 0 && payload.edits.every(e => e && e.room_authority === 'manual');
  if (payload.revision !== state.revision && !manualForce) throw new Error('DB가 변경되었습니다. 새로고침 후 다시 선택해 주세요.');
  if (['collect', 'scan'].includes(kind)) {
    const keys = state.entries.filter(e => !e.archived).map(e => e.key);
    if (payload.all !== true && (!Array.isArray(payload.keys) || !payload.keys.length || payload.keys.some(k => !keys.includes(k)))) throw new Error('수집할 항목을 선택해 주세요.');
  }
  const repo = setting_('GH_REPO');
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) throw new Error('GH_REPO 설정 오류');
  const token = setting_('GH_TOKEN');
  const branch = props_().getProperty('GH_BRANCH') || 'main';
  const job = {id: requestId, kind: kind, payload: payload, state: 'queued',
    message: 'GitHub 실행 대기 중', started_at: iso_(), heartbeat: iso_(), logs: [],
    base_manifest: setting_('CURRENT_MANIFEST_ID'), total: 0, completed: 0, nonces: [], stopRequested: false};
  saveJob_(job);
  props_().setProperty('ACTIVE_JOB_ID', job.id);
  const ids = JSON.parse(props_().getProperty('RECENT_JOB_IDS') || '[]');
  props_().setProperty('RECENT_JOB_IDS', JSON.stringify([job.id].concat(ids).slice(0, 30)));
  try {
    const response = UrlFetchApp.fetch('https://api.github.com/repos/' + repo + '/actions/workflows/collect.yml/dispatches', {
      method: 'post', contentType: 'application/json', muteHttpExceptions: true,
      headers: {Authorization: 'Bearer ' + token, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
      payload: JSON.stringify({ref: branch, inputs: {job_id: job.id,
        needs_browser: String(!['apply', 'archive', 'room-rules', 'approve-review'].includes(kind) || (['apply', 'approve-review'].includes(kind) && payload.collect_after === true))}})
    });
    if (![200, 204].includes(response.getResponseCode())) {
      Object.assign(job, {state: 'failed', message: 'GitHub 실행 요청 실패 (' + response.getResponseCode() + '). 저장소와 Actions 권한을 확인해 주세요.', finished_at: iso_()});
      saveJob_(job);
      props_().deleteProperty('ACTIVE_JOB_ID');
      throw new Error(job.message);
    }
  } catch (error) {
    if (job.state !== 'failed') {
      job.message = 'GitHub 요청 응답 확인 중. 중복 실행하지 않고 실행 상태를 기다립니다.';
      saveJob_(job);
    } else throw error;
  }
  return {job_id: job.id, queued: true};
}

function decode_(body) {
  if (typeof body !== 'string' || body.length > 16000000) throw new Error('Invalid envelope');
  const env = JSON.parse(body);
  if (!Number.isInteger(env.timestamp) || Math.abs(Date.now() / 1000 - env.timestamp) > 300 || !UUID.test(env.nonce || '')) throw new Error('Expired envelope');
  if (typeof env.payload !== 'string' || !/^[a-f0-9]{64}$/.test(env.signature || '')) throw new Error('Invalid signature');
  const secret = setting_('BRIDGE_SECRET');
  if (secret.length < 32) throw new Error('Bridge not configured');
  const expected = hex_(Utilities.computeHmacSha256Signature(env.timestamp + '\n' + env.nonce + '\n' + env.payload, secret));
  if (!same_(expected, env.signature)) throw new Error('Invalid signature');
  const payload = JSON.parse(Utilities.newBlob(Utilities.base64Decode(env.payload)).getDataAsString('UTF-8'));
  return {payload: payload, nonce: env.nonce};
}
function doPost(event) {
  let result, decoded;
  const context = {stage: 'authenticate'};
  try {
    decoded = decode_(event && event.postData && event.postData.contents);
    context.stage = 'lock';
    result = {ok: true, value: locked_(() => worker_(decoded.payload, decoded.nonce, context))};
  } catch (error) {
    let code = 'AUTH_REJECTED', message = '수집기 인증에 실패했습니다. 연결 설정을 확인해 주세요.', retryable = false;
    if (decoded) {
      code = error.bridgeCode || 'INTERNAL_ERROR';
      message = error.bridgeCode ? error.message : '클라우드 내부 처리 오류입니다. 오류 코드와 참조번호를 확인해 주세요.';
      retryable = error.retryable === true;
      if (!error.bridgeCode) {
        const detail = String(error.message || '');
        if (/too many times in a short time|service unavailable|temporarily|try again|internal error|timed out|timeout/i.test(detail)) {
          code = 'SERVICE_TEMPORARY'; message = 'Google 서비스 응답이 지연되어 자동으로 다시 연결합니다.'; retryable = true;
        } else if (/quota|limit exceeded|too many times|maximum execution time/i.test(detail)) {
          code = 'SERVICE_LIMIT'; message = 'Google 서비스 사용 한도에 도달했습니다. 잠시 후 다시 실행해 주세요.';
        }
      }
    }
    const reference = Utilities.getUuid();
    // Log only controlled metadata, never credentials, envelopes or workbook contents.
    console.error(JSON.stringify({event: 'worker_error', reference: reference, code: code,
      stage: context.stage, retryable: retryable}));
    result = {ok: false, code: code, error: message, retryable: retryable, stage: context.stage, reference: reference};
  }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

function progress_(job, progress) {
  ['completed', 'total', 'normal', 'issues'].forEach(k => {
    if (Number.isInteger(progress[k]) && progress[k] >= 0 && progress[k] <= 100000) job[k] = progress[k];
  });
  if (typeof progress.message === 'string') job.message = progress.message.slice(0, 600);
  if (Array.isArray(progress.logs)) job.logs = progress.logs.slice(-150).map(l => ({time: String(l.time).slice(0, 30), text: String(l.text).slice(0, 600)}));
  job.heartbeat = iso_();
}

function worker_(payload, nonce, context) {
  context = context || {};
  context.stage = 'load_job';
  const job = recoverJob_(findJob_(payload.job_id));
  if (!job) throw bridgeError_('JOB_NOT_FOUND', '작업 기록을 찾지 못했습니다. 화면을 새로고침해 주세요.');
  if (job.nonces.includes(nonce)) throw bridgeError_('REPLAY_REJECTED', '이미 처리된 인증 요청입니다.');
  job.nonces = job.nonces.concat([nonce]).slice(-512);
  context.stage = 'save_nonce';
  saveJob_(job);
  if (payload.action === 'claim') {
    if (TERMINAL.includes(job.state)) return {terminal: true};
    if (props_().getProperty('ACTIVE_JOB_ID') !== job.id) throw bridgeError_('JOB_NOT_ACTIVE', '현재 실행 중인 작업이 아닙니다.');
    if (!/^[0-9]+:[0-9]+$/.test(payload.execution || '')) throw bridgeError_('INVALID_EXECUTION', '수집기 실행번호가 올바르지 않습니다.');
    if (job.execution && job.execution !== payload.execution) throw bridgeError_('ALREADY_CLAIMED', '다른 수집기가 이미 처리 중인 작업입니다.');
    if (!job.lease) {
      Object.assign(job, {state: 'running', execution: payload.execution,
        lease: Utilities.getUuid() + Utilities.getUuid(), heartbeat: iso_(), message: '클라우드 수집기 연결됨'});
      context.stage = 'save_claim';
      saveJob_(job);
    }
    context.stage = 'read_bundle';
    const manifest = json_(job.base_manifest);
    const bundle = DriveApp.getFileById(manifest.bundle_id).getBlob().getBytes();
    return {lease: job.lease, job: {kind: job.kind, payload: job.payload}, bundle: Utilities.base64Encode(bundle), sha256: manifest.sha256};
  }
  if (!same_(job.lease, payload.lease) || job.execution !== payload.execution) throw bridgeError_('INVALID_LEASE', '이 작업을 처리할 실행 권한이 일치하지 않습니다.');
  if (payload.action === 'commit' && current_().job_id === job.id) return {saved: true};
  if (TERMINAL.includes(job.state) || props_().getProperty('ACTIVE_JOB_ID') !== job.id) throw bridgeError_('JOB_NOT_ACTIVE', '종료되거나 중단된 작업입니다. 화면에서 새 작업을 시작해 주세요.');
  if (payload.action === 'progress') {
    context.stage = 'save_progress';
    progress_(job, payload.progress || {});
    saveJob_(job);
    return {stopRequested: job.stopRequested};
  }
  if (payload.action === 'fail') {
    context.stage = 'save_failure';
    Object.assign(job, {state: 'failed', message: String(payload.message || '수집 실패').slice(0, 600), finished_at: iso_()});
    saveJob_(job);
    props_().deleteProperty('ACTIVE_JOB_ID');
    return {recorded: true};
  }
  if (payload.action !== 'commit') throw bridgeError_('INVALID_ACTION', '지원하지 않는 수집기 요청입니다.');
  if (setting_('CURRENT_MANIFEST_ID') !== job.base_manifest) throw bridgeError_('STALE_COMMIT', '새 자료가 이미 저장되어 이전 실행의 덮어쓰기를 차단했습니다.');
  if (!['completed', 'issues', 'stopped'].includes((payload.progress || {}).state)) throw bridgeError_('INVALID_OUTCOME', '저장할 작업 결과가 올바르지 않습니다.');
  context.stage = 'validate_bundle';
  const bytes = Utilities.base64Decode(payload.bundle);
  if (!same_(sha_(bytes), payload.sha256)) throw bridgeError_('CHECKSUM_MISMATCH', '전송된 결과의 무결성 검증에 실패했습니다.');
  context.stage = 'store_bundle';
  const manifest = storeBundle_(bytes, job.id, payload.progress, payload.response || {});
  // This pointer is the only publication step. Earlier snapshots remain unchanged.
  props_().setProperty('CURRENT_MANIFEST_ID', manifest.id);
  context.stage = 'publish_completion';
  progress_(job, payload.progress);
  Object.assign(job, {state: payload.progress.state, response: payload.response || {}, finished_at: iso_()});
  saveJob_(job);
  props_().deleteProperty('ACTIVE_JOB_ID');
  return {saved: true};
}

function storeBundle_(bytes, jobId, outcome, response) {
  if (bytes.length > MAX_ZIP) throw new Error('Bundle too large');
  const blob = Utilities.newBlob(bytes, 'application/zip', 'snapshot.zip');
  const parts = Utilities.unzip(blob);
  const names = parts.map(p => p.getName());
  if (names.length !== ROLES.length || new Set(names).size !== names.length || names.some(n => !ROLES.includes(n))) throw new Error('Invalid bundle files');
  if (parts.reduce((n, p) => n + p.getBytes().length, 0) > MAX_EXPANDED) throw new Error('Bundle too large');
  const statePart = parts.find(p => p.getName() === 'state.json');
  const state = JSON.parse(statePart.getDataAsString('UTF-8'));
  if (!Array.isArray(state.entries) || !same_(state.revision, sha_(parts.find(p => p.getName() === 'db.xlsx').getBytes()))) throw new Error('DB revision mismatch');
  ['history.json', 'catalog.json', 'workspace.json'].forEach(n => JSON.parse(parts.find(p => p.getName() === n).getDataAsString('UTF-8')));
  const folder = DriveApp.getFolderById(setting_('SNAPSHOTS_FOLDER_ID')).createFolder(Utilities.formatDate(new Date(), 'Asia/Seoul', 'yyyyMMdd_HHmmss') + '_' + jobId);
  const files = {};
  parts.forEach(p => {
    const role = p.getName();
    if (role === 'db.xlsx') p.setName('객실_DB.xlsx');
    if (role === 'result.xlsx') p.setName('예약현황_Result.xlsx');
    p.setContentType(role.endsWith('.xlsx') ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'application/json');
    files[role] = folder.createFile(p).getId();
  });
  const manifest = {schema: 1, job_id: jobId, created_at: iso_(), bundle_id: folder.createFile(blob).getId(),
    sha256: sha_(bytes), files: files, outcome: outcome, response: response};
  const id = saveJson_(folder, 'manifest.json', manifest);
  return {id: id};
}

// Run once in the Apps Script editor after setting the documented properties.
function setup() {
  owner_();
  return locked_(() => {
    if (props_().getProperty('CURRENT_MANIFEST_ID')) return '이미 초기화되어 있습니다. 기존 자료를 유지합니다.';
    const folder = root_();
    if (!props_().getProperty('SNAPSHOTS_FOLDER_ID')) props_().setProperty('SNAPSHOTS_FOLDER_ID', folder.createFolder('데이터_백업').getId());
    if (!props_().getProperty('JOBS_FOLDER_ID')) props_().setProperty('JOBS_FOLDER_ID', folder.createFolder('작업기록').getId());
    if (!props_().getProperty('BRIDGE_SECRET')) props_().setProperty('BRIDGE_SECRET', Utilities.getUuid() + Utilities.getUuid());
    const bytes = DriveApp.getFileById(setting_('SEED_FILE_ID')).getBlob().getBytes();
    const manifest = storeBundle_(bytes, 'initial', {state: 'completed', message: '기존 자료 가져오기 완료'}, {});
    props_().setProperty('CURRENT_MANIFEST_ID', manifest.id);
    return '초기 자료 저장 완료';
  });
}
