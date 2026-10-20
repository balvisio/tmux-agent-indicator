local module = dofile(arg[1])
local now, jobs, timers, writes, restarts
local function reset()
    now, jobs, timers, writes, restarts = 0, {}, {}, {}, 0
end
reset()
hs = {
    json = {decode = function(output)
        if output == 'bad' then error('invalid JSON') end
        return {enabled = output == 'on', user = 'fixture-user'}
    end},
    timer = {
        secondsSinceEpoch = function() return now end,
        doAfter = function(delay, callback)
            local t = {at = now + delay, callback = callback, active = true, stop = function(self) self.active = false end}
            timers[#timers + 1] = t
            return t
        end,
    },
    task = {new = function(path, callback, args)
        local job = {path = path, callback = callback, args = args,
            start = function(self) jobs[#jobs + 1] = self; return true end,
            terminate = function(self) self.terminated = true end}
        return job
    end},
}
local function complete(code, output, err)
    local job = assert(table.remove(jobs, 1), 'no helper running')
    if job.path == '/usr/bin/osascript' then
        assert(job.args[1] == '-l' and job.args[2] == 'JavaScript' and job.args[3] == '/fixture/slack-badge.js')
        writes[#writes + 1] = job.args[4]
        output = output or job.args[4]
    else
        assert(job.path == '/usr/bin/pkill')
        assert(table.concat(job.args, ' ') == '-TERM -u fixture-user -x usernoted', 'signal only this user and notification daemon')
        restarts = restarts + 1
    end
    job.callback(code or 0, output or '', err or '')
    return job
end
local function advance(seconds)
    now = now + seconds
    for _, t in ipairs(timers) do
        if t.active and now >= t.at then t.active = false; t.callback() end
    end
end
local function new() return module.new('/fixture/slack-badge.js') end
local badge = new()
badge.sync('dnd'); complete(); complete()
assert(writes[1] == 'off' and restarts == 1 and not badge.status())
for _ = 1, 10 do badge.sync('dnd') end
assert(#jobs == 0 and restarts == 1, 'unchanged mode must not repeat service restarts')
badge.sync('on'); complete(); complete()
assert(writes[2] == 'on' and restarts == 2)

reset(); badge = new()
badge.sync('dnd'); badge.sync('on'); complete()
assert(restarts == 0 and #jobs == 1, 'coalesce a stale preference change before restarting')
complete(); complete()
assert(writes[1] == 'off' and writes[2] == 'on' and restarts == 1)
badge.sync('dnd'); complete(); badge.sync('on'); complete(); complete(); complete()
assert(writes[#writes] == 'on', 'mode change during daemon refresh must eventually apply')

reset(); badge = new()
badge.sync('dnd'); complete(1, '', 'permission denied')
assert(badge.status():find('permission denied') and restarts == 0)
badge.sync('dnd'); assert(#jobs == 0, 'back off on failure')
advance(60); badge.sync('dnd'); complete(); complete()
assert(not badge.status(), 'retry must clear the error')

reset(); badge = new()
badge.sync('dnd'); complete(); complete(1)
assert(not badge.status(), 'daemon not running is safe; it will load the synchronized preference')
badge.sync('on'); complete(); complete(1, '', 'Operation not permitted')
assert(badge.status():find('notification service'), 'do not hide a signalling failure')

reset(); badge = new()
badge.sync('dnd'); complete(0, 'bad')
assert(badge.status():find('invalid') and restarts == 0, 'validate helper response before signalling')

reset(); badge = new()
badge.sync('dnd'); local stalled = jobs[1]; advance(11)
assert(stalled.terminated and badge.status():find('timed out'))
complete()
assert(#jobs == 0 and restarts == 0, 'ignore a late callback from the timed-out attempt')
badge.sync('on'); complete(); complete()
assert(not badge.status() and writes[#writes] == 'on', 'mode changes retry immediately')

reset(); badge = new(); badge.sync('on'); complete(); complete()
local reloaded = new(); reloaded.sync('on'); complete(); complete()
assert(restarts == 2, 'reload reconciles an interrupted change even when the preference is already set')
print('PASS: direct Slack badge transitions, rapid toggles, failures, scoped service refresh, timeouts, retries, and reloads')
