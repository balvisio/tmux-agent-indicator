#!/usr/bin/env node
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function main() {
    const source = fs.readFileSync(path.join(__dirname, '../plugins/opencode-tmux-agent-indicator.js'), 'utf8');
    const context = vm.createContext({process});
    vm.runInContext(source.replace('export const TmuxAgentIndicator', 'this.plugin'), context);
    const calls = [];
    const pending = [];
    const hooks = await context.plugin({$: (_parts, script, state) => {
        calls.push(state);
        assert(script.endsWith('/scripts/agent-state.sh'));
        assert.equal(pending.length, 0, 'state updates must never overlap');
        return new Promise((resolve, reject) => pending.push({resolve, reject}));
    }});
    const tick = () => new Promise(resolve => setImmediate(resolve));
    const event = (type, properties = {}) => hooks.event({event: {type, properties}});
    const finish = async (failed = false) => {
        const request = pending.shift();
        assert(request, 'expected a running update');
        if (failed) request.reject(new Error('tmux unavailable'));
        else request.resolve();
        await tick();
    };
    const promptly = async promise => {
        const timeout = new Promise((_, reject) => {
            const timer = setTimeout(() => reject(new Error('OpenCode waited for tmux')), 1000);
            timer.unref();
        });
        await Promise.race([promise, timeout]);
    };

    await promptly(event('session.status', {status: {type: 'busy'}}));
    await promptly(hooks['permission.ask']());
    await promptly(event('permission.asked'));
    await promptly(event('session.idle'));
    assert.deepEqual(calls, ['running'], 'no off reset and no overtaking the first update');
    await finish();
    assert.deepEqual(calls, ['running', 'needs-input']);
    await finish();
    assert.deepEqual(calls, ['running', 'needs-input', 'done'], 'duplicates are skipped after success');
    await finish();
    await promptly(event('session.status', {status: {type: 'busy'}}));
    await tick();
    assert.equal(calls.length, 3, 'preserve the existing late-busy event guard');

    await promptly(hooks['tool.execute.before']({tool: 'question'}));
    await finish(true);
    await promptly(event('permission.updated'));
    await tick();
    assert.equal(calls.at(-1), 'needs-input');
    assert.equal(calls.length, 5, 'a failed state must be retryable');
    await finish();
    await promptly(event('session.error'));
    await finish();
    assert.equal(calls.at(-1), 'done');
    await promptly(event('permission.asked'));
    await finish(true);
    const before = calls.length;
    await promptly(event('session.error'));
    await tick();
    assert.equal(calls.length, before + 1, 'after a partial failure even the previous state must be reapplied');
    await finish();
    assert.equal(pending.length, 0);
    console.log('PASS: OpenCode handlers return promptly, serialize updates, skip duplicates, and retry failures');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
