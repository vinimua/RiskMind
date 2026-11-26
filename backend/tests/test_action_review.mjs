import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const html = readFileSync(new URL('../src/risk_platform/api/action_review.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]+)<\/script>/)[1];
function page({ confirmed = true } = {}) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: '', textContent: '', disabled: false, hidden: false });
    return elements.get(id);
  };
  const calls = [];
  let status = 'PENDING_CONFIRMATION';
  const context = vm.createContext({
    document: { getElementById: element }, window: { confirm: () => confirmed },
    fetch: async (path, options) => {
      calls.push({ path, options });
      if (options.method === 'POST') status = 'SUCCEEDED';
      return { ok: true, json: async () => options.method === 'POST' ? { round_complete: true } : {
        action_id: 'action-fixed', tool_name: 'test_send', investigation_id: 'I', task_id: 'T',
        expires_at: '2030-01-01', status, digest: 'a'.repeat(64),
        request: { target: 'approved-target' }, material: { text: '<script>untrusted</script>' }, audit: [],
      } };
    }, URL, setTimeout,
  });
  vm.runInContext(script, context);
  element('token').value = 'test-only-token';
  element('actionId').value = 'action-fixed';
  return { element, calls };
}

test('approval submits only saved action identity and digest, never edited form target', async () => {
  const { element, calls } = page();
  await element('load').onclick();
  element('actionId').value = 'action-forged';
  await element('approve').onclick();
  const post = calls.find(call => call.options.method === 'POST');
  assert.equal(post.path, '/actions/action-fixed/confirmation');
  assert.deepEqual(JSON.parse(post.options.body), { digest: 'a'.repeat(64), approved: true });
  assert.equal(post.options.headers.Authorization, 'Bearer test-only-token');
  assert.ok(calls.every(call => !call.path.includes('test-only-token')));
  assert.equal(element('approve').disabled, true);
});

test('cancelling browser confirmation sends no decision and leaves a clear status', async () => {
  const { element, calls } = page({ confirmed: false });
  await element('load').onclick();
  await element('approve').onclick();
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
  assert.equal(element('message').textContent, '已取消确认，尚未提交决定。');
});

test('empty token is rejected before any business request', async () => {
  const { element, calls } = page();
  element('token').value = '';
  await element('load').onclick();
  assert.equal(calls.length, 0);
  assert.equal(element('message').textContent, '请输入访问令牌。');
});
