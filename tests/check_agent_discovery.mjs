// Optional consumer check. No model request or account credentials are needed.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';

const source = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dependencies = path.resolve(process.argv[2] ?? '.qa-agent-deps/node_modules');
const workspace = await mkdtemp(path.join(tmpdir(), 'douyin-agent-qa-'));
const skill = path.join(workspace, '.agents', 'skills', 'douyin-downloader');
const manifest = path.join(skill, 'SKILL.md');

async function codexDiscovery() {
  const launcher = path.join(dependencies, '@openai/codex/bin/codex.js');
  const child = spawn(process.execPath, [launcher, 'app-server', '--stdio'], {
    cwd: workspace, stdio: ['pipe', 'pipe', 'pipe'],
  });
  const closed = new Promise(resolve => child.once('close', resolve));
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-8000); });
  const lines = readline.createInterface({ input: child.stdout });
  const send = value => child.stdin.write(JSON.stringify(value) + '\n');
  let timer;
  try {
    const response = await new Promise((resolve, reject) => {
      timer = setTimeout(() => reject(new Error('Codex skills/list timed out: ' + stderr)), 30000);
      child.once('error', reject);
      child.once('exit', code => reject(new Error(`Codex exited ${code}: ${stderr}`)));
      lines.on('line', line => {
        let value;
        try { value = JSON.parse(line); } catch { return; }
        if (value.error) { reject(new Error(JSON.stringify(value.error))); return; }
        if (value.id === 0) {
          send({ method: 'initialized', params: {} });
          send({ id: 1, method: 'skills/list', params: { cwds: [workspace], forceReload: true } });
        } else if (value.id === 1) resolve(value.result);
      });
      send({ id: 0, method: 'initialize', params: {
        clientInfo: { name: 'douyin_skill_qa', version: '1.0' },
        capabilities: { experimentalApi: true },
      } });
    });
    const candidates = response.data.flatMap(row => row.skills).filter(row => row.name === 'douyin-downloader');
    const expected = await realpath(manifest);
    let found = false;
    for (const row of candidates) {
      if (await realpath(row.path) === expected && row.enabled) found = true;
    }
    assert.ok(found, 'Codex did not discover the project .agents skill');
    const info = JSON.parse(await readFile(path.join(dependencies, '@openai/codex/package.json'), 'utf8'));
    return { consumer: 'Codex', version: info.version, discovered: true, enabled: true };
  } finally {
    clearTimeout(timer);
    lines.close();
    child.stdin.end();
    child.kill();
    await closed;
  }
}

async function deepseekDiscovery() {
  const entry = path.join(dependencies, '@deepseek-ai/dsh-skill-filesystem/lib/index.js');
  const { FileSystemSkillProvider } = await import(pathToFileURL(entry));
  const warnings = [];
  const controller = new AbortController();
  const context = { get: () => undefined, logger: { warn: text => warnings.push(String(text)) } };
  const provider = new FileSystemSkillProvider(context, { signal: controller.signal, invalidate() {} }, {
    dshHome: path.join(workspace, 'empty-dsh-home'),
    agentsHome: path.join(workspace, 'empty-agents-home'), watch: false,
  });
  try {
    const observation = await provider.list({ cwd: workspace });
    const candidates = Array.isArray(observation) ? observation : observation.candidates;
    const candidate = candidates.find(row => row.name === 'douyin-downloader');
    assert.ok(candidate, 'DeepSeek did not discover the project .agents skill');
    const loaded = await provider.get(candidate, { cwd: workspace });
    assert.equal(loaded.invocation.modelInvocable, true);
    assert.equal(loaded.invocation.userInvocable, true);
    assert.equal(loaded.resourceBase.kind, 'directory');
    assert.equal(await realpath(loaded.resourceBase.path), await realpath(skill));
    assert.ok(loaded.content.includes('--share-text'));
    assert.ok((await readFile(path.join(loaded.resourceBase.path, 'scripts/start_dashboard.py'), 'utf8')).length);
    assert.deepEqual(warnings, []);
    const info = JSON.parse(await readFile(path.join(dependencies, '@deepseek-ai/dsh-skill-filesystem/package.json'), 'utf8'));
    return { consumer: 'DeepSeek Harness skill provider', version: info.version,
      discovered: true, modelInvocable: true, userInvocable: true, relativeScripts: true };
  } finally {
    await provider.dispose();
    controller.abort();
  }
}

try {
  for (const name of ['SKILL.md', 'scripts/start_dashboard.py', 'scripts/download_douyin.py']) {
    await mkdir(path.dirname(path.join(skill, name)), { recursive: true });
    await copyFile(path.join(source, name), path.join(skill, name));
  }
  console.log(JSON.stringify({ platform: process.platform,
    checks: [await codexDiscovery(), await deepseekDiscovery()] }, null, 2));
} finally {
  await rm(workspace, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
}
