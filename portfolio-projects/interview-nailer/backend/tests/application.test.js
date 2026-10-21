const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { randomUUID } = require('node:crypto');
const root = path.resolve(__dirname, '..', '.test-data');
const folder = path.join(root, randomUUID());
fs.mkdirSync(folder, { recursive: true });
process.env.NODE_ENV = 'test';
process.env.STORAGE_MODE = process.env.TEST_STORAGE_MODE || 'file';
process.env.AI_MODE = 'mock';
process.env.JWT_SECRET = 'test-only-signing-key-never-for-production';
process.env.STORE_FILE = path.join(folder, 'store.json');
const { createApp } = require('../server');
const store = require('../services/store');
const { createAI } = require('../config/ai');
const { loadConfig } = require('../config/env');
const jwt = require('jsonwebtoken');
const password = 'local test password 123';
let server, base, owner, other, resume, session;

before(async () => {
  await store.initialize();
  server = await new Promise(resolve => { const s = createApp({ limits: { auth: 100, ai: 100, resume: 100 } }).listen(0, '127.0.0.1', () => resolve(s)); });
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  if (server) await new Promise(resolve => server.close(resolve));
  const { pool } = require('../config/db');
  if (pool) await pool.end();
  assert.equal(path.dirname(path.resolve(folder)), root);
  fs.rmSync(folder, { recursive: true, force: true });
});
async function request(endpoint, body, token, method) {
  const response = await fetch(base + endpoint, { method: method || (body ? 'POST' : 'GET'), headers: {
    'Content-Type': 'application/json', Origin: 'http://localhost:5000', ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }, ...(body ? { body: JSON.stringify(body) } : {}) });
  return { status: response.status, body: await response.json() };
}

test('configuration fails closed for invalid modes and production secrets', () => {
  assert.throws(() => loadConfig({ AI_MODE: 'typo' }), /AI_MODE/);
  assert.throws(() => loadConfig({ STORAGE_MODE: 'typo' }), /STORAGE_MODE/);
  assert.throws(() => loadConfig({ AI_MODE: 'anthropic' }), /API_KEY/);
  assert.throws(() => loadConfig({ NODE_ENV: 'production' }), /JWT_SECRET/);
});

test('provider validates output and sanitizes upstream failures without network calls', async () => {
  const provider = response => createAI({ mode: 'anthropic', client: { messages: { create: async () => response } } });
  for (const response of [{ stop_reason: 'max_tokens', content: [] }, { stop_reason: 'end_turn', content: [{ type: 'text', text: 'invalid json' }] },
    { stop_reason: 'end_turn', content: [{ type: 'text', text: '{"questions":[]}' }] }]) {
    await assert.rejects(provider(response).callAI('prompt', 10, 'questions'), error => error.status === 502);
  }
  const unavailable = createAI({ mode: 'anthropic', client: { messages: { create: async () => { throw new Error('private upstream details'); } } } });
  await assert.rejects(unavailable.callAI('prompt', 10, 'questions'), error => error.status === 503 && !error.message.includes('private'));
});

test('built frontend serves assets and client routes while preserving API responses', async (t) => {
  const build = fs.mkdtempSync(path.join(os.tmpdir(), 'interview-frontend-'));
  t.after(() => fs.rmSync(build, { recursive: true, force: true }));
  const html = '<!doctype html><title>Interview test frontend</title>';
  const script = 'window.frontendLoaded = true;';
  fs.writeFileSync(path.join(build, 'index.html'), html);
  fs.writeFileSync(path.join(build, 'app.js'), script);
  const app = createApp({ build });
  const frontend = await new Promise(resolve => { const s = app.listen(0, '127.0.0.1', () => resolve(s)); });
  const url = `http://127.0.0.1:${frontend.address().port}`;
  try {
    for (const route of ['/', '/register', '/sessions/example/coaching']) {
      const response = await fetch(url + route);
      assert.equal(response.status, 200, route);
      assert.match(response.headers.get('content-type'), /text\/html/);
      assert.equal(await response.text(), html);
    }
    const asset = await fetch(url + '/app.js');
    assert.equal(asset.status, 200);
    assert.equal(await asset.text(), script);
    const health = await fetch(url + '/api/status');
    assert.equal(health.status, 200);
    assert.equal((await health.json()).status, 'ok');
    for (const [route, method] of [['/api/missing', 'GET'], ['/register', 'POST']]) {
      const response = await fetch(url + route, { method });
      assert.equal(response.status, 404);
      assert.deepEqual(await response.json(), { error: 'Route not found.' });
    }
  } finally { await new Promise(resolve => frontend.close(resolve)); }
});

test('registration, login and expired tokens', async () => {
  const rejectedOrigin = await fetch(base + '/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', Origin: 'https://untrusted.example' }, body: '{}' });
  assert.equal(rejectedOrigin.status, 403);
  const email = `owner-${randomUUID()}@example.test`;
  const created = await request('/api/auth/register', { email, password, full_name: 'Demo owner' });
  assert.equal(created.status, 201, JSON.stringify(created.body)); owner = created.body;
  other = (await request('/api/auth/register', { email: `other-${randomUUID()}@example.test`, password })).body;
  assert.equal((await request('/api/auth/register', { email, password })).status, 409);
  assert.equal((await request('/api/auth/login', { email, password })).status, 200);
  assert.equal((await request('/api/auth/login', { email, password: 'incorrect' })).status, 401);
  assert.equal((await request('/api/sessions')).status, 401);
  const expired = jwt.sign({ id: owner.user.id, email }, process.env.JWT_SECRET, { expiresIn: -1 });
  assert.equal((await request('/api/sessions', undefined, expired)).status, 401);
});

test('text resume upload and matching work in mock mode', async () => {
  const form = new FormData();
  form.append('resume', new Blob(['Demo engineer. Built and tested Node APIs with PostgreSQL. Improved documentation and worked with teammates on reliable software. '.repeat(3)], { type: 'text/plain' }), 'demo.txt');
  const response = await fetch(base + '/api/resume/upload', { method: 'POST', headers: { Authorization: `Bearer ${owner.token}` }, body: form });
  const body = await response.json();
  assert.equal(response.status, 200, JSON.stringify(body)); resume = body.resume;
  assert.ok(resume.id);
  assert.equal((await request('/api/resume/match', { resume_id: resume.id, job_role: 'Software engineer' }, owner.token)).status, 200);
  assert.equal((await request('/api/resume/match', { resume_id: resume.id, job_role: 'Software engineer' }, other.token)).status, 404);
});

test('session ownership rejects foreign resumes including legacy associations', async () => {
  assert.ok(resume);
  assert.equal((await request('/api/sessions/start', { job_role: 'Engineer', resume_id: resume.id }, other.token)).status, 404);
  const legacy = await store.createSession({ userId: other.user.id, resumeId: resume.id, jobRole: 'Engineer', mode: 'mock' });
  assert.equal((await request(`/api/sessions/${legacy.id}/complete`, {}, other.token)).status, 404);
  const started = await request('/api/sessions/start', { job_role: 'Engineer', resume_id: resume.id }, owner.token);
  assert.equal(started.status, 200, JSON.stringify(started.body));
  assert.ok(started.body.questions.length > 0); session = started.body.session;
  assert.equal((await request(`/api/sessions/${session.id}`, undefined, other.token)).status, 404);
  assert.equal((await request(`/api/sessions/${session.id}/answer`, { question: 'A question', user_answer: 'An answer' }, other.token)).status, 404);
});

test('complete mock interview: generate, score, save, coach and revisit', async () => {
  const input = { job_role: 'Engineer', question: 'Tell me about a technical challenge.', user_answer: 'I diagnosed a failing API, added tests and fixed the race condition.', resume_id: resume.id };
  const answer = await request('/api/answers/generate', input, owner.token);
  assert.equal(answer.status, 200, JSON.stringify(answer.body));
  const score = await request('/api/answers/score', input, owner.token);
  assert.equal(score.status, 200, JSON.stringify(score.body));
  const saved = await request(`/api/sessions/${session.id}/answer`, { question: input.question, user_answer: input.user_answer, ai_answer: answer.body.answer, score: score.body }, owner.token);
  assert.equal(saved.status, 200, JSON.stringify(saved.body));
  const completed = await request(`/api/sessions/${session.id}/complete`, {}, owner.token);
  assert.equal(completed.status, 200, JSON.stringify(completed.body));
  assert.ok(completed.body.coaching.weekly_plan.length);
  assert.equal((await request(`/api/sessions/${session.id}/complete`, {}, owner.token)).status, 200);
  assert.equal((await request(`/api/sessions/${session.id}/answer`, { question: 'More?', user_answer: 'No' }, owner.token)).status, 409);
  const streamed = await fetch(base + '/api/answers/generate/stream', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${owner.token}` }, body: JSON.stringify(input) });
  assert.equal(streamed.status, 200); assert.match(await streamed.text(), /data: \[DONE\]/);
});

test('concurrent writes retain every account and reject duplicate email races', async () => {
  const prefix = randomUUID();
  const users = await Promise.all(Array.from({ length: 16 }, (_, i) => store.createUser({ email: `${prefix}-${i}@example.test`, passwordHash: 'test-hash', fullName: 'Concurrent' })));
  for (const user of users) assert.equal((await store.findUserByEmail(user.email)).id, user.id);
  const duplicates = await Promise.allSettled(Array.from({ length: 4 }, () => store.createUser({ email: `${prefix}-duplicate@example.test`, passwordHash: 'test-hash' })));
  assert.equal(duplicates.filter(result => result.status === 'fulfilled').length, 1);
});

test('authentication rate limit returns 429', async () => {
  const limited = await new Promise(resolve => { const s = createApp({ limits: { auth: 1 } }).listen(0, '127.0.0.1', () => resolve(s)); });
  try {
    const url = `http://127.0.0.1:${limited.address().port}/api/auth/login`;
    const options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' };
    assert.equal((await fetch(url, options)).status, 400);
    assert.equal((await fetch(url, options)).status, 429);
  } finally { await new Promise(resolve => limited.close(resolve)); }
});
