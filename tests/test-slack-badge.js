const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const clone = x => JSON.parse(JSON.stringify(x));

function fixture(options = {}) {
    const initial = {version: 1, otherSetting: 'keep', apps: [
        {'bundle-id': 'other.app', flags: 17492353038, metadata: ['keep']},
        {'bundle-id': 'com.tinyspeck.slackmacgap', flags: 17492353038, auth: 1, metadata: {unknown: 'keep'}},
    ]};
    const state = {disk: clone(initial), cache: clone(initial.apps), writes: 0, reads: 0};
    class Native {
        constructor(data) { this.data = data; }
        get count() { return String(this.data.length); }
        get integerValue() { return String(this.data); }
        get unsignedLongLongValue() { return String(this.data); }
        get mutableCopy() { return new Native(Array.isArray(this.data) ? [...this.data] : {...this.data}); }
        objectForKey(key) { return new Native(this.data[key]); }
        objectAtIndex(index) { return new Native(this.data[index]); }
        isKindOfClass(type) {
            if (type === 'array') return Array.isArray(this.data);
            if (type.kind === 'dictionary') return this.data !== null && typeof this.data === 'object' && !Array.isArray(this.data);
            return typeof this.data === 'number';
        }
        isEqualToArray(other) { return JSON.stringify(this.data) === JSON.stringify(other.data); }
        isEqualToDictionary(other) {
            try { assert.deepEqual(this.data, other.data); return true; } catch { return false; }
        }
        setObjectForKey(value, key) { this.data[key] = value.data; }
        removeObjectForKey(key) { delete this.data[key]; }
        replaceObjectAtIndexWithObject(index, value) { this.data[index] = value.data; }
    }
    const $ = data => new Native(data);
    Object.assign($, {
        NSArray: 'array', NSNumber: {numberWithUnsignedLongLong: value => new Native(value)},
        NSProcessInfo: {processInfo: {operatingSystemVersion: {majorVersion: '26'}}},
        NSHomeDirectory: () => new Native('/fixture'), NSUserName: () => new Native('fixture-user'),
        NSURL: {fileURLWithPath: value => new Native(value)},
        NSUserDefaults: {alloc: {_initWithSuiteNameContainer: () => ({
            get synchronize() { return true; },
            objectForKey: () => new Native(state.cache),
            setObjectForKey: value => { state.writes++; state.cache = clone(value.data); state.disk.apps = clone(value.data); },
        })}},
        NSMutableDictionary: {get dictionary() { return new Native({}); }},
        NSDictionary: {kind: 'dictionary', dictionaryWithContentsOfFile: () => {
            state.reads++;
            if (options.concurrent && state.reads === 2) state.disk.apps[0].flags += 4;
            return new Native(clone(state.disk));
        }},
    });
    const context = { $, ObjC: {import: () => {}, unwrap: value => value.data}, Number, JSON };
    vm.createContext(context); vm.runInContext(source, context);
    return {state, context, initial, apply: mode => JSON.parse(context.run([mode]))};
}

let f = fixture();
assert.equal(f.apply('off').enabled, false);
assert.equal(f.state.disk.apps[1].flags, 17492353036, 'retain bits above bit 31');
assert.deepEqual(f.state.disk.apps[0], f.initial.apps[0], 'leave other applications unchanged');
assert.deepEqual(f.state.disk.apps[1].metadata, f.initial.apps[1].metadata);
assert.equal(f.state.disk.otherSetting, 'keep');
f.apply('on'); assert.deepEqual(f.state.disk, f.initial, 'round trip restores only the badge bit');
f.apply('on'); assert.equal(f.state.writes, 2, 'skip unchanged preference writes');

f = fixture(); f.state.cache.reverse();
f.state.cache[0].src = {stale: 'value'};
f.state.disk.apps[1].src = {current: 'value'};
f.state.cache[0].push = {token: 'old', other: 'keep'};
f.state.disk.apps[1].push = {token: 'new', other: 'keep'};
f.apply('off');
assert.deepEqual(f.state.disk.apps[1].src, {current: 'value'}, 'retain fresh registration metadata');
assert.deepEqual(f.state.disk.apps[1].push, {token: 'new', other: 'keep'});

f = fixture({concurrent: true});
assert.throws(() => f.apply('off'), /changed/);
assert.equal(f.state.writes, 0, 'do not overwrite a concurrent settings edit');
f = fixture(); f.state.cache[0].flags += 4;
assert.throws(() => f.apply('off'), /cache differs/); assert.equal(f.state.writes, 0);
f = fixture(); f.state.disk.version = 2;
assert.throws(() => f.apply('off'), /compatibility/); assert.equal(f.state.writes, 0);
f = fixture(); f.context.$.NSProcessInfo.processInfo.operatingSystemVersion.majorVersion = '27';
assert.throws(() => f.apply('off'), /macOS 26/); assert.equal(f.state.writes, 0);
f = fixture(); f.state.disk.apps.pop(); f.state.cache.pop();
assert.throws(() => f.apply('off'), /not found/); assert.equal(f.state.writes, 0);
f = fixture(); f.state.disk.apps.push(clone(f.state.disk.apps[1])); f.state.cache = clone(f.state.disk.apps);
assert.throws(() => f.apply('off'), /duplicate/i); assert.equal(f.state.writes, 0);
assert.throws(() => f.context.badgeFlags(Number.MAX_SAFE_INTEGER + 1, false), /Unsupported/);
console.log('PASS: direct badge preference preserves other settings, wide flags, and concurrent edits; rejects unknown formats');
