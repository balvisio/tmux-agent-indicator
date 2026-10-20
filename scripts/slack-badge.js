// Run with osascript -l JavaScript. The container initializer is a private macOS API.
// Keep Objective-C objects intact so notification registration metadata is preserved.
function badgeFlags(flags, enabled) {
    if (!Number.isSafeInteger(flags) || flags < 0) throw Error('Unsupported notification flags');
    const present = flags % 4 >= 2;
    // JavaScript bitwise operators would discard flags above bit 31.
    return flags + (enabled === present ? 0 : enabled ? 2 : -2);
}

function nativeKind(value, type) {
    return value && typeof value.isKindOfClass === 'function' && value.isKindOfClass(type);
}

function samePreferences(left, right) {
    function normalize(apps) {
        if (!nativeKind(apps, $.NSArray)) throw Error('Unsupported application records');
        const result = $.NSMutableDictionary.dictionary;
        const seen = new Set();
        for (let i = 0; i < apps.count; i++) {
            const record = apps.objectAtIndex(i).mutableCopy;
            const id = ObjC.unwrap(record.objectForKey('bundle-id'));
            if (typeof id !== 'string' || seen.has(id)) throw Error('Missing or duplicate notification entry');
            seen.add(id);
            // Registration sources and push tokens can lag in the preference cache. Preserve
            // their current disk values when writing; compare all other fields strictly.
            record.removeObjectForKey('src');
            const push = record.objectForKey('push');
            if (nativeKind(push, $.NSDictionary)) {
                const metadata = push.mutableCopy;
                metadata.removeObjectForKey('token');
                record.setObjectForKey(metadata, 'push');
            }
            result.setObjectForKey(record, id);
        }
        return result;
    }
    return normalize(left).isEqualToDictionary(normalize(right));
}

function run(args) {
    if (args.length !== 1 || !['on', 'off'].includes(args[0])) throw Error('Expected on or off');
    ObjC.import('Foundation');
    if (Number($.NSProcessInfo.processInfo.operatingSystemVersion.majorVersion) !== 26) {
        throw Error('Direct Slack badge control currently supports macOS 26');
    }
    const home = ObjC.unwrap($.NSHomeDirectory());
    const directory = home + '/Library/Group Containers/group.com.apple.usernoted';
    const path = directory + '/Library/Preferences/group.com.apple.usernoted.plist';
    const preferences = $.NSUserDefaults.alloc._initWithSuiteNameContainer(
        'group.com.apple.usernoted', $.NSURL.fileURLWithPath(directory));
    if (!preferences.synchronize) throw Error('Cannot synchronize notification preferences');
    const disk = $.NSDictionary.dictionaryWithContentsOfFile(path);
    if (!nativeKind(disk, $.NSDictionary) || Number(disk.objectForKey('version').integerValue) !== 1) {
        throw Error('Cannot read notification preferences; check Full Disk Access and macOS compatibility');
    }
    const apps = disk.objectForKey('apps');
    if (!samePreferences(apps, preferences.objectForKey('apps'))) {
        throw Error('Notification preferences changed or their cache differs; retry later');
    }
    let index = -1;
    for (let i = 0; i < apps.count; i++) {
        if (ObjC.unwrap(apps.objectAtIndex(i).objectForKey('bundle-id')) === 'com.tinyspeck.slackmacgap') {
            if (index !== -1) throw Error('Duplicate Slack notification entries');
            index = i;
        }
    }
    if (index === -1) throw Error('Slack notification settings were not found');
    const record = apps.objectAtIndex(index);
    const flags = record.objectForKey('flags');
    if (!nativeKind(flags, $.NSNumber)) throw Error('Unsupported Slack notification flags');
    const enabled = args[0] === 'on';
    const current = Number(flags.unsignedLongLongValue);
    const next = badgeFlags(current, enabled);
    if (next !== current) {
        const updatedRecord = record.mutableCopy;
        updatedRecord.setObjectForKey($.NSNumber.numberWithUnsignedLongLong(next), 'flags');
        const updatedApps = apps.mutableCopy;
        updatedApps.replaceObjectAtIndexWithObject(index, updatedRecord);
        // Check again immediately before replacing the shared applications array.
        const fresh = $.NSDictionary.dictionaryWithContentsOfFile(path);
        if (!fresh.objectForKey('apps').isEqualToArray(apps)) throw Error('Notification preferences changed; retry later');
        preferences.setObjectForKey(updatedApps, 'apps');
        if (!preferences.synchronize) throw Error('Could not save Slack badge preference');
        const verified = $.NSDictionary.dictionaryWithContentsOfFile(path).objectForKey('apps');
        if (!samePreferences(verified, updatedApps)) throw Error('Could not verify Slack badge preference');
    }
    return JSON.stringify({enabled: enabled, user: ObjC.unwrap($.NSUserName())});
}
