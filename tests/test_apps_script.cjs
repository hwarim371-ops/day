/* Apps Script service boundaries are mocked; crypto and application code are real. */
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../cloud/apps_script/Code.gs'), 'utf8');
const sha = bytes => crypto.createHash('sha256').update(Buffer.from(bytes)).digest('hex');
const secret = 'test-only-secret-not-valid-in-production-123456789';
const requestId = () => crypto.randomUUID();

function harness() {
  const props = new Map(), files = new Map(), cache = new Map();
  let counter = 0, clock = Date.now(), failCreate = false, failJobWrite = false, dispatchCode = 204, dispatches = 0;
  let lockBusy = false, lockHeld = false;
  const reads = [], errors = [];
  class Blob {
    constructor(bytes, type = '', name = '') { this.bytes = Buffer.from(bytes); this.type = type; this.name = name; }
    getBytes() { return [...this.bytes]; }
    getDataAsString() { return this.bytes.toString('utf8'); }
    getName() { return this.name; }
    setName(name) { this.name = name; return this; }
    setContentType(type) { this.type = type; return this; }
  }
  class File {
    constructor(name, bytes) { this.id = String(++counter); this.name = name; this.bytes = Buffer.from(bytes); files.set(this.id, this); }
    getId() { return this.id; }
    getBlob() { reads.push({name: this.name, locked: lockHeld}); return new Blob(this.bytes, '', this.name); }
    setContent(text) {
      if (failJobWrite && this.name.endsWith('.json') && /^[a-f0-9]{8}-/.test(this.name)) throw Error('simulated write failure');
      this.bytes = Buffer.from(text); return this;
    }
  }
  class Folder extends File {
    constructor(name) { super(name, ''); this.children = []; }
    createFolder(name) { const folder = new Folder(name); this.children.push(folder); return folder; }
    createFile(name, text) {
      if (failCreate) throw Error(typeof failCreate === 'string' ? failCreate : 'simulated Drive outage');
      const file = name instanceof Blob ? new File(name.name, name.bytes) : new File(name, text);
      this.children.push(file); return file;
    }
    getFilesByName(name) {
      const matches = this.children.filter(f => !(f instanceof Folder) && f.name === name);
      return {hasNext: () => matches.length > 0, next: () => matches.shift()};
    }
  }
  const root = new Folder('test root');
  const db = Buffer.from('mock workbook bytes');
  const state = {revision: sha(db), entries: [{key: 'a'.repeat(20), rooms: ['A-1'], archived: false}], jobs: [], job: {state: 'idle'}};
  const bundle = Buffer.from(JSON.stringify({'db.xlsx': [...db], 'result.xlsx': [1, 2, 3],
    'catalog.json': [...Buffer.from('{}')], 'workspace.json': [...Buffer.from('{}')],
    'state.json': [...Buffer.from(JSON.stringify(state))], 'history.json': [...Buffer.from('{}')]}));
  const seed = root.createFile(new Blob(bundle, 'application/zip', 'seed.zip'));
  props.set('OWNER_EMAIL', 'owner@example.com'); props.set('ROOT_FOLDER_ID', root.id);
  props.set('SEED_FILE_ID', seed.id); props.set('BRIDGE_SECRET', secret);
  props.set('GH_REPO', 'example/camping'); props.set('GH_TOKEN', 'test-token');
  let email = 'owner@example.com';
  const context = vm.createContext({
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [clock])); } static now() { return clock; } },
    PropertiesService: {getScriptProperties: () => ({getProperty: k => props.get(k) || null,
      setProperty: (k, v) => props.set(k, v), deleteProperty: k => props.delete(k)})},
    LockService: {getScriptLock: () => ({tryLock: () => {if (lockBusy) return false; assert.equal(lockHeld, false); lockHeld = true; return true;}, releaseLock: () => {lockHeld = false;}})},
    console: {error: value => errors.push(JSON.parse(value))},
    CacheService: {getScriptCache: () => ({get: key => cache.get(key), put: (key, value) => cache.set(key, value), remove: key => cache.delete(key)})},
    Session: {getActiveUser: () => ({getEmail: () => email})},
    DriveApp: {getFileById: id => {if (!files.has(id)) throw Error('missing file'); return files.get(id);}, getFolderById: id => files.get(id)},
    Utilities: {
      DigestAlgorithm: {SHA_256: 'SHA_256'}, getUuid: requestId,
      computeDigest: (_, bytes) => [...crypto.createHash('sha256').update(Buffer.from(bytes)).digest()],
      computeHmacSha256Signature: (text, key) => [...crypto.createHmac('sha256', key).update(text).digest()],
      base64Decode: text => [...Buffer.from(text, 'base64')], base64Encode: bytes => Buffer.from(bytes).toString('base64'),
      newBlob: (bytes, type, name) => new Blob(bytes, type, name),
      unzip: blob => Object.entries(JSON.parse(blob.bytes.toString())).map(([name, bytes]) => new Blob(bytes, '', name)),
      formatDate: () => '2026-09-11'
    },
    UrlFetchApp: {fetch: () => {dispatches++; return {getResponseCode: () => dispatchCode};}},
    ContentService: {MimeType: {JSON: 'json'}, createTextOutput: text => ({text, setMimeType() {return this;}})},
    HtmlService: {createHtmlOutput: text => text, createHtmlOutputFromFile: name => ({name, setTitle() {return this;}, addMetaTag() {return this;}})}
  });
  vm.runInContext(code, context);
  context.setup();
  function env(payload, nonce = requestId(), timestamp = Math.floor(clock / 1000)) {
    const data = Buffer.from(JSON.stringify(payload)).toString('base64');
    return {timestamp, nonce, payload: data, signature: crypto.createHmac('sha256', secret).update(`${timestamp}\n${nonce}\n${data}`).digest('hex')};
  }
  const postEnvelope = e => JSON.parse(context.doPost({postData: {contents: JSON.stringify(e)}}).text);
  const post = (payload, nonce) => postEnvelope(env(payload, nonce));
  const start = (id = requestId()) => {
    const payload = {kind: 'collect', date: '2026-09-11', all: true, revision: state.revision};
    return {id, payload, result: context.uiRequest('/api/start', payload, id)};
  };
  const claim = id => post({action: 'claim', job_id: id, execution: '100:1'});
  const action = (id, lease, verb, extra = {}) => post({action: verb, job_id: id, execution: '100:1', lease, ...extra});
  return {context, props, files, state, bundle, env, post, postEnvelope, start, claim, action,
    setEmail: value => email = value, advance: ms => clock += ms,
    failCreate: value => failCreate = value, failJobWrite: value => failJobWrite = value,
    dispatchCode: value => dispatchCode = value, dispatchCount: () => dispatches,
    lockBusy: value => lockBusy = value, reads, errors};
}

let tests = 0;
function test(name, fn) { fn(); tests++; console.log('PASS ' + name); }
test('confirmed review dispatches one authenticated combined job', () => {
  const h = harness(), id = requestId();
  const payload = {revision: h.state.revision, collect_after: true, date: '2026-09-27', decisions: []};
  assert.equal(h.context.uiRequest('/api/approve-review', payload, id).ok, true);
  const claim = h.claim(id);
  assert.equal(claim.ok, true);
  assert.equal(claim.value.job.kind, 'approve-review');
  assert.equal(claim.value.job.payload.collect_after, true);
  assert.equal(h.dispatchCount(), 1);
});
test('review approval never uses the manual revision bypass', () => {
  const h = harness();
  const response = h.context.uiRequest('/api/approve-review', {revision: 'old', force_manual: true,
    edits: [{room_authority: 'manual'}]}, requestId());
  assert.equal(response.ok, false);
  assert.equal(h.dispatchCount(), 0);
});
test('explicit manual edits can dispatch against a newer DB without bypassing owner checks', () => {
  const h = harness();
  const payload = {revision: 'old', force_manual: true, edits: [{key: 'a'.repeat(20), room_authority: 'manual', rooms: ['A-1']}]};
  h.setEmail('not-owner@example.com');
  assert.equal(h.context.uiRequest('/api/apply', payload, requestId()).ok, false);
  assert.equal(h.dispatchCount(), 0);
  h.setEmail('owner@example.com');
  assert.equal(h.context.uiRequest('/api/apply', payload, requestId()).ok, true);
  assert.equal(h.dispatchCount(), 1);
});
test('automatic and mixed stale edits cannot use manual force', () => {
  const h = harness();
  for (const edits of [[], [{room_authority: 'auto'}], [{room_authority: 'manual'}, {}]]) {
    assert.equal(h.context.uiRequest('/api/apply', {revision: 'old', force_manual: true, edits}, requestId()).ok, false);
  }
  assert.equal(h.dispatchCount(), 0);
});
test('manual force cannot start over another active job', () => {
  const h = harness();
  h.start();
  assert.equal(h.context.uiRequest('/api/apply', {revision: 'old', force_manual: true,
    edits: [{room_authority: 'manual', rooms: ['A-1']}]}, requestId()).ok, false);
  assert.equal(h.dispatchCount(), 1);
});
test('unchanged idle polling does not reread Drive history or snapshots', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  h.action(id, lease, 'commit', commitPayload(h));
  const initial = h.context.uiRequest('/api/state').value;
  h.reads.length = 0;
  const next = h.context.uiRequest('/api/state', {manifest: initial.manifest, jobs_key: initial.jobs_key}).value;
  assert.equal(next.jobs, undefined);
  assert.equal(next.job.id, id);
  assert.equal(next.job.remote_log, true);
  assert.equal(next.job.logs, undefined);
  assert.equal(h.reads.length, 0);
});
test('compact editor projection keeps identity and removes large raw page text', () => {
  const h = harness();
  const value = h.context.compactState_({entries: [{item_groups: {room: [
    {itemKey: 'one', title: 'A-1', decision: 'room', reason: 'room', mode: 'auto', text: 'large private page text'}]}}]});
  assert.equal(value.entries[0].item_groups.room[0].text, undefined);
  assert.equal(value.entries[0].item_groups.room[0].itemKey, 'one');
});
test('initial view loads one recent job and full history remains available on demand', () => {
  const h = harness();
  for (let n = 0; n < 3; n++) {
    const {id} = h.start(), lease = h.claim(id).value.lease;
    h.action(id, lease, 'fail', {message: 'fixture'});
  }
  assert.equal(h.context.uiRequest('/api/state').value.jobs.length, 1);
  assert.equal(h.context.uiRequest('/api/jobs').value.jobs.length, 3);
});
test('owner identity is required even on anonymous deployment', () => {
  const h = harness();
  for (const email of ['', 'someone@example.com']) {
    h.setEmail(email);
    assert.equal(h.context.uiRequest('/api/state').ok, false);
    assert.match(h.context.doGet(), /로그인/);
    assert.throws(() => h.context.setup());
  }
});
test('setup is idempotent and does not replace existing data', () => {
  const h = harness(), before = h.props.get('CURRENT_MANIFEST_ID');
  h.context.setup(); assert.equal(h.props.get('CURRENT_MANIFEST_ID'), before);
});
test('old revisions and invalid selections cannot enqueue work', () => {
  const h = harness();
  assert.equal(h.context.uiRequest('/api/start', {kind: 'collect', revision: 'old', all: true}, requestId()).ok, false);
  assert.equal(h.context.uiRequest('/api/start', {kind: 'collect', revision: h.state.revision, keys: ['bad']}, requestId()).ok, false);
  assert.equal(h.dispatchCount(), 0);
});
test('one active job and idempotent dispatch', () => {
  const h = harness(), first = h.start();
  assert.equal(first.result.ok, true);
  assert.equal(h.start().result.ok, false);
  assert.equal(h.context.uiRequest('/api/start', first.payload, first.id).ok, true);
  assert.equal(h.dispatchCount(), 1);
});
test('GitHub authorization failure records failure without changing data', () => {
  const h = harness(), pointer = h.props.get('CURRENT_MANIFEST_ID');
  h.dispatchCode(403); assert.equal(h.start().result.ok, false);
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), pointer);
  assert.equal(h.props.has('ACTIVE_JOB_ID'), false);
});
test('tampered, expired and replayed HMAC envelopes rejected', () => {
  const h = harness(), {id} = h.start();
  const body = h.env({action: 'claim', job_id: id, execution: '100:1'});
  assert.equal(h.postEnvelope({...body, signature: '0'.repeat(64)}).ok, false);
  assert.equal(h.postEnvelope({...body, timestamp: 1}).ok, false);
  assert.equal(h.postEnvelope(body).ok, true);
  assert.equal(h.postEnvelope(body).ok, false);
});
test('claim retry works but another execution cannot take the lease', () => {
  const h = harness(), {id} = h.start(), claim = h.claim(id);
  assert.equal(h.claim(id).value.lease, claim.value.lease);
  assert.equal(h.post({action: 'claim', job_id: id, execution: '101:1'}).ok, false);
  assert.equal(h.action(id, 'bad-lease', 'progress').ok, false);
});
test('queued stop never launches collection', () => {
  const h = harness(), {id} = h.start();
  assert.equal(h.context.uiRequest('/api/stop', {}).ok, true);
  assert.equal(h.claim(id).value.terminal, true);
});
test('running stop reaches worker heartbeat', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  h.context.uiRequest('/api/stop', {});
  assert.equal(h.action(id, lease, 'progress').value.stopRequested, true);
});
function commitPayload(h) {
  return {bundle: h.bundle.toString('base64'), sha256: sha(h.bundle), response: {keys: ['a'.repeat(20)]},
    progress: {id: 'local-worker-id', state: 'issues', message: '확인 필요 1건', completed: 1, total: 1}};
}
test('commit verifies hash and publishes only complete snapshots', () => {
  const h = harness(), pointer = h.props.get('CURRENT_MANIFEST_ID'), {id} = h.start(), lease = h.claim(id).value.lease;
  const input = commitPayload(h);
  assert.equal(h.action(id, lease, 'commit', {...input, sha256: '0'.repeat(64)}).ok, false);
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), pointer);
  assert.equal(h.action(id, lease, 'commit', input).value.saved, true);
  assert.notEqual(h.props.get('CURRENT_MANIFEST_ID'), pointer);
  assert.equal(h.context.uiRequest('/api/state').value.job.state, 'issues');
});
test('lost completion response is idempotent, not a recollection', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  assert.equal(h.action(id, lease, 'commit', commitPayload(h)).ok, true);
  const pointer = h.props.get('CURRENT_MANIFEST_ID');
  assert.equal(h.action(id, lease, 'commit', commitPayload(h)).value.saved, true);
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), pointer);
  assert.equal(h.claim(id).value.terminal, true);
});
test('Drive outage before publication preserves canonical pointer', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease, pointer = h.props.get('CURRENT_MANIFEST_ID');
  h.failCreate(true);
  assert.equal(h.action(id, lease, 'commit', commitPayload(h)).ok, false);
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), pointer);
});
test('crash after publication recovers outcome without replacing cloud job ID', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  // Simulate the narrower crash: canonical manifest already published, job remains running.
  const old = h.context.findJob_(id);
  h.action(id, lease, 'commit', commitPayload(h));
  h.context.saveJob_(old);
  h.props.set('ACTIVE_JOB_ID', id);
  const state = h.context.uiRequest('/api/state').value;
  assert.equal(state.job.id, id);
  assert.equal(state.job.state, 'issues');
  assert.equal(state.busy, false);
});
test('missing heartbeat invalidates late worker commits', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease, pointer = h.props.get('CURRENT_MANIFEST_ID');
  h.advance(6 * 60 * 1000);
  assert.equal(h.context.uiRequest('/api/state').value.job.state, 'interrupted');
  assert.equal(h.action(id, lease, 'commit', commitPayload(h)).ok, false);
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), pointer);
});
test('failed job keeps prior result and reports the failure', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease, pointer = h.props.get('CURRENT_MANIFEST_ID');
  h.action(id, lease, 'fail', {message: '수집기 오류'});
  const state = h.context.uiRequest('/api/state').value;
  assert.equal(state.job.state, 'failed'); assert.equal(state.job.message, '수집기 오류');
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), pointer);
});
test('unchanged snapshots do not resend the entire catalog', () => {
  const h = harness();
  const initial = h.context.uiRequest('/api/state').value;
  const next = h.context.uiRequest('/api/state', {manifest: initial.manifest}).value;
  assert.equal(next.not_modified, true);
  assert.equal(next.entries, undefined);
  assert.equal(next.manifest, initial.manifest);
});
test('history list is compact and details are loaded separately', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  h.action(id, lease, 'progress', {progress: {logs: [{time: '2026-09-11T09:00:00', text: 'test progress'}]}});
  const state = h.context.uiRequest('/api/state').value;
  assert.equal(state.jobs[0].logs, undefined);
  assert.equal(state.jobs[0].remote_log, true);
  assert.equal(h.context.uiRequest('/api/job', {id}).value.logs.length, 1);
});
test('lock contention is retryable and cannot grant a lease', () => {
  const h = harness(), {id} = h.start();
  h.lockBusy(true);
  const result = h.claim(id);
  assert.equal(result.code, 'BUSY'); assert.equal(result.retryable, true);
  assert.equal(h.context.findJob_(id).lease, undefined);
  h.lockBusy(false);
  assert.equal(h.claim(id).ok, true);
});
test('authentication and lease failures remain non-retryable', () => {
  const h = harness(), {id} = h.start();
  const env = h.env({action: 'claim', job_id: id, execution: '100:1'});
  const denied = h.postEnvelope({...env, signature: '0'.repeat(64)});
  assert.equal(denied.code, 'AUTH_REJECTED'); assert.equal(denied.retryable, false);
  h.claim(id);
  const wrongLease = h.action(id, 'bad-lease', 'progress');
  assert.equal(wrongLease.code, 'INVALID_LEASE'); assert.equal(wrongLease.retryable, false);
});
test('temporary Drive failure retries commit without replacing old data', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  const before = h.props.get('CURRENT_MANIFEST_ID');
  h.failCreate('Service unavailable: try again later');
  const failed = h.action(id, lease, 'commit', commitPayload(h));
  assert.equal(failed.code, 'SERVICE_TEMPORARY'); assert.equal(failed.retryable, true);
  assert.equal(failed.stage, 'store_bundle');
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'), before);
  h.failCreate(false);
  assert.equal(h.action(id, lease, 'commit', commitPayload(h)).value.saved, true);
});
test('unexpected errors have a safe reference without leaking exception text', () => {
  const h = harness(), {id} = h.start(), lease = h.claim(id).value.lease;
  h.failCreate('private-user-data-do-not-log');
  const result = h.action(id, lease, 'commit', commitPayload(h));
  assert.equal(result.code, 'INTERNAL_ERROR'); assert.equal(result.retryable, false);
  assert.ok(result.reference);
  assert.doesNotMatch(JSON.stringify([result, h.errors]), /private-user-data-do-not-log|test-only-secret/);
});
test('large read-only snapshots and history are read outside the worker lock', () => {
  const h = harness();
  h.reads.length = 0;
  assert.equal(h.context.uiRequest('/api/state').ok, true);
  const stateReads = h.reads.filter(r => r.name === 'state.json');
  assert.ok(stateReads.length); assert.ok(stateReads.every(r => !r.locked));
  h.lockBusy(true);
  assert.equal(h.context.uiRequest('/api/history?key=' + 'a'.repeat(20)).ok, true);
});
test('owner-only order writes no job or workbook and survives snapshots and cached polling', () => {
  const h = harness(), keys = ['a','b','c'].map(x=>x.repeat(20));
  const manifestId = h.props.get('CURRENT_MANIFEST_ID'), manifest = h.context.current_();
  h.files.get(manifest.files['state.json']).setContent(JSON.stringify({...h.state, entries:keys.map(key=>({key,archived:false}))}));
  const payload = {revision:h.state.revision, order_revision:'', keys:keys.slice().reverse()};
  h.setEmail('other@example.com');
  assert.equal(h.context.uiRequest('/api/list-order',payload).ok,false);
  h.setEmail('owner@example.com');
  const before = h.files.get(manifest.files['db.xlsx']).getBlob().getDataAsString();
  const saved = h.context.uiRequest('/api/list-order',payload);
  assert.equal(saved.ok,true);
  assert.deepEqual(Array.from(saved.value.display_order),payload.keys);
  assert.equal(h.props.get('CURRENT_MANIFEST_ID'),manifestId);
  assert.equal(h.dispatchCount(),0);
  assert.equal(h.files.get(manifest.files['db.xlsx']).getBlob().getDataAsString(),before);
  const state = h.context.uiRequest('/api/state').value;
  h.reads.length = 0;
  const next = h.context.uiRequest('/api/state',{manifest:state.manifest,jobs_key:state.jobs_key}).value;
  assert.equal(next.not_modified,true);
  assert.deepEqual(Array.from(next.display_order),payload.keys);
  assert.equal(h.reads.length,0);
  h.files.get(manifest.files['state.json']).setContent(JSON.stringify(h.state));
  assert.deepEqual(Array.from(h.context.uiRequest('/api/state').value.display_order),payload.keys);
});
test('list order rejects stale, malformed, missing or duplicate keys without replacing the saved order', () => {
  const h=harness(), key='a'.repeat(20), payload={revision:h.state.revision,order_revision:'',keys:[key]};
  for(const keys of [[],[key,key],['unknown'],null,[{}]]) assert.equal(h.context.uiRequest('/api/list-order',{...payload,keys}).ok,false);
  assert.equal(h.context.uiRequest('/api/list-order',{...payload,revision:'old'}).ok,false);
  const saved=h.context.uiRequest('/api/list-order',payload).value;
  assert.equal(h.context.uiRequest('/api/list-order',payload).ok,false);
  assert.equal(h.context.uiRequest('/api/state').value.order_revision,saved.order_revision);
});
test('list order caches invalidate for other devices and do not overwrite an active collection', () => {
  const h=harness(), {id}=h.start();
  const payload={revision:h.state.revision,order_revision:'',keys:['a'.repeat(20)]};
  const first=h.context.uiRequest('/api/list-order',payload).value;
  const file=h.props.get('LIST_ORDER_FILE_ID');
  const second=h.context.uiRequest('/api/list-order',{...payload,order_revision:first.order_revision}).value;
  assert.notEqual(first.order_revision,second.order_revision);
  assert.equal(h.props.get('LIST_ORDER_FILE_ID'),file);
  assert.equal(h.context.uiRequest('/api/state').value.order_revision,second.order_revision);
  assert.equal(h.props.get('ACTIVE_JOB_ID'),id);
  assert.equal(h.dispatchCount(),1);
});
console.log(`${tests} Apps Script boundary tests passed (Google services mocked).`);
