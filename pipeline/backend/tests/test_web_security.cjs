// Offline regression: execute only the copied page script with a minimal DOM/API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
// Skip only the automatic initial requests; all page functions remain unchanged.
const source = script.slice(0, script.lastIndexOf('\nloadSettings().then('));
const hostileKey = `shot');globalThis.pwned=true;//"><img src=x onerror="globalThis.pwned=true">`;
const decode = text => text.replace(/&(quot|#39|lt|gt|amp);/g,
  (_, entity) => ({quot:'"', '#39':"'", lt:'<', gt:'>', amp:'&'}[entity]));

function page() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', innerHTML:'', textContent:'', style:{},
      classList:{add(){}, remove(){}, contains(){return false;}}, addEventListener(){}});
    return nodes.get(id);
  };
  const context = vm.createContext({URL, document:{getElementById:node, querySelectorAll:()=>[]},
    setInterval(){}, prompt:()=>null, alert(){}, pwned:false});
  vm.runInContext(source, context, {filename:'copied-web-index.js'});
  return {context, node, run:code=>vm.runInContext(code, context)};
}

test('switching to local replaces official defaults and requires a new service key', () => {
  const p = page();
  p.run('settings={provider:"official"}');
  p.node('setH3').value='local';
  p.node('setH3Base').value='https://api.minimax.cn';
  p.node('setH3Key').value='old-official-key';
  assert.equal(typeof p.context.onH3ProviderChange, 'function');
  p.context.onH3ProviderChange();
  assert.equal(p.node('setH3Base').value,'http://127.0.0.1:30010');
  assert.equal(p.node('setH3Key').value,'');
  assert.equal(String(p.node('setConcurrency').value),'1');
  assert.equal(String(p.node('setRetry').value),'0');
  assert.equal(p.node('setM3Review').checked,false);
});

test('switching to local preserves a deliberately configured custom service origin', () => {
  const p = page();
  p.run('settings={provider:"mock"}');
  p.node('setH3').value='local';
  p.node('setH3Base').value='https://video.example.org';
  assert.equal(typeof p.context.onH3ProviderChange, 'function');
  p.context.onH3ProviderChange();
  assert.equal(p.node('setH3Base').value,'https://video.example.org');
});

function storyboard(p, key) {
  p.context.fixture = {status:'confirmed', version:1, storyboard_id:7,
    shots:[{shot_key:key, purpose:'test', duration:4, mode:'reference', action:''}]};
  p.run('currentSb=fixture; renderCardForm=()=>{}; selectShot=key=>{globalThis.chosen=key}; renderSb()');
}

test('storyboard shot keys remain text in the duration bar and list', () => {
  const p = page();
  storyboard(p, hostileKey);
  for (const id of ['durationBar', 'shotList']) {
    const output = p.node(id).innerHTML;
    assert.ok(!output.includes('<img'), `${id} must not create an injected element`);
    assert.ok(output.includes('&lt;img'), `${id} must retain the literal key`);
  }
});

test('clicking a quoted shot key passes the exact key without executing it', () => {
  const p = page();
  const key = "shot');globalThis.pwned=true;//";
  storyboard(p, key);
  const handler = p.node('shotList').innerHTML.match(/onclick="([^"]*)"/)[1];
  p.context.chosen = null;
  p.run(decode(handler));
  assert.equal(p.context.pwned, false);
  assert.equal(p.context.chosen, key);
});

test('QC issues escape model-derived shot keys', () => {
  const p = page();
  p.context.issues = [{shot_key:hostileKey, severity:'blocker', message:'test'}];
  p.run('renderQc(issues, "Review")');
  assert.ok(!p.node('qcIssues').innerHTML.includes('<img'));
  assert.ok(p.node('qcIssues').innerHTML.includes('&lt;img'));
});

test('project board and take list escape shot keys and show explicit task binding', async () => {
  const p = page();
  const take = {id:8, take_id:8, shot_key:hostileKey, status:'unknown', attempt_no:1,
    duration:4, mode:'reference', can_bind_task:true};
  p.context.api = async url => url.includes('/board')
    ? {storyboard_id:7, shots:[take]} : url.startsWith('/api/takes') ? [take] : [];
  p.run('loadHealth=async()=>{}');
  await p.context.loadBoard();
  for (const id of ['projectShots', 'takes']) {
    assert.ok(!p.node(id).innerHTML.includes('<img'), id);
    assert.ok(p.node(id).innerHTML.includes('&lt;img'), id);
  }
  assert.match(p.node('takes').innerHTML, /onclick="bindTask\(8\)"/);
});

test('shot save and rewrite encode the key as a single URL segment', async () => {
  const p = page();
  const key = 'shot/a?next=#中文';
  const calls = [];
  p.context.key = key;
  p.context.api = async url => {calls.push(url); return {storyboard_id:7};};
  p.run('selectedShot=key; currentSb={storyboard_id:7}; shotFormPayload=()=>({}); renderSb=()=>{}');
  await p.context.saveShot();
  await p.context.rewriteShot();
  const segment = encodeURIComponent(key);
  assert.deepEqual(calls, [`/api/storyboards/7/shots/${segment}`, `/api/storyboards/7/shots/${segment}/rewrite`]);
});

test('manual binding sends only the supplied task ID and refreshes the board', async () => {
  const p = page();
  const calls = [];
  p.context.prompt = () => '  exact-task-id  ';
  p.context.api = async (url, options) => {calls.push({url, ...options}); return {};};
  p.run('loadBoard=async()=>{globalThis.refreshed=true}');
  assert.equal(typeof p.context.bindTask, 'function');
  await p.context.bindTask(8);
  assert.deepEqual(calls, [{url:'/api/takes/8/bind-task', method:'POST',
    body:JSON.stringify({task_id:'exact-task-id'})}]);
  assert.equal(p.context.refreshed, true);
});

test('cancelling or leaving manual binding empty never submits', async () => {
  const p = page();
  p.context.api = async () => {assert.fail('No request is allowed without an exact task ID');};
  assert.equal(typeof p.context.bindTask, 'function');
  await p.context.bindTask(8);
  p.context.prompt = () => '   ';
  await p.context.bindTask(8);
});
