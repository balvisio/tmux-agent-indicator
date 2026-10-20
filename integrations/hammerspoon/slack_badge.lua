-- Serialize preference changes and notification-service refreshes. Keep the latest requested
-- mode when a helper is still finishing an earlier change.
local M = {}

function M.new(script)
    local desired, applied, task, timeout, notice
    local busy, retryAt, generation = false, 0, 0
    local sync
    local function finish(target, reason)
        busy = false
        notice = reason and ("Slack badge: " .. reason) or nil
        if reason then
            applied, retryAt = nil, hs.timer.secondsSinceEpoch() + 60
            print("[agent_dots] " .. notice)
        else
            applied, retryAt = target, 0
        end
        if desired ~= target then retryAt = 0; sync(desired) end
    end
    local function execute(path, args, target, callback)
        generation = generation + 1
        local token = generation
        task = hs.task.new(path, function(code, output, err)
            if token ~= generation then return end
            if timeout then timeout:stop(); timeout = nil end
            task = nil
            callback(code, output, err)
        end, args)
        if not task or not task:start() then finish(target, "could not start helper"); return end
        timeout = hs.timer.doAfter(10, function()
            generation = generation + 1
            if task then task:terminate(); task = nil end
            timeout = nil
            finish(target, "helper timed out")
        end)
    end
    sync = function(mode)
        if mode ~= desired then desired, retryAt = mode, 0 end
        if busy or applied == desired or hs.timer.secondsSinceEpoch() < retryAt then return end
        local target = desired
        busy, applied = true, nil
        execute("/usr/bin/osascript", {"-l", "JavaScript", script, target == "dnd" and "off" or "on"}, target,
            function(code, output, err)
                if code ~= 0 then finish(target, (err or "preference change failed"):gsub("%s+$", "")); return end
                local ok, result = pcall(hs.json.decode, output)
                if not ok or type(result) ~= "table" or type(result.user) ~= "string" or result.user == ""
                    or result.enabled ~= (target ~= "dnd") then
                    finish(target, "invalid preference-helper response"); return
                end
                if desired ~= target then busy = false; sync(desired); return end
                -- Refresh even after an unchanged write: an interrupted attempt may have saved
                -- the preference without refreshing usernoted. Signal only this user's daemon.
                execute("/usr/bin/pkill", {"-TERM", "-u", result.user, "-x", "usernoted"}, target,
                    function(status, _, errors)
                        local noProcess = status == 1 and (errors or ""):match("^%s*$")
                        if status == 0 or noProcess then finish(target)
                        else finish(target, "could not refresh the notification service") end
                    end)
            end)
    end
    return {sync = sync, status = function() return notice end}
end

return M
