-- agent_dots.lua
-- Menu bar dots for tmux windows that hold an AI agent (Claude / Codex), coloured like the tmux
-- window list. Colors come from the visible title style, including custom palettes. ◉ marks the current window.
--
--   T: ◉ ● ●  |  VS - colossus - kernelcatcher : ●  |  SSH - colossus : ●   00:42
--   local tmux   VS Code hosts…                        ssh windows' hosts…   stopwatch (HH:MM since the last mode switch)
--
-- Local segment: this Mac's tmux. Then one segment per remote host, from two sources:
--   VS   hosts VS Code is connected to through Remote-SSH (its own `ssh … <host>` processes)
--   SSH  hosts of the `ssh …` windows in the local tmux (@ssh_host, kept by ~/.tmux/agent-present.sh)
-- Each host is polled once every REMOTE_REFRESH seconds over our own ssh connection by a probe script
-- that looks at the host's tmux and at the tmux inside every running VS Code dev container. One
-- segment per place under VS: "VS - host" for the host's own tmux, "VS - host - container" for a dev
-- container. SSH shows only the host's own tmux: "SSH - host : ○" when it has no agent windows,
-- regardless of agents in containers. A host under both sources is still polled once. A visible
-- remote window that flips to needs-input / done plays the same Mac sound as a local one
-- (~/.tmux/agent-sound.sh).
--
-- Click for a menu: the mode switch (On / Do Not Disturb) on top, then the agent windows per place;
-- picking one selects it in its tmux and focuses iTerm (local; SSH also selects the ssh window) or
-- VS Code (VS).
-- Wide titles summarize agent windows by source (T / VS / SSH), keeping the clock visible.
-- Click > / < after the clock to expand/collapse; click elsewhere to open the agent menu.
--
-- Modes:
--   On              dots as above, sounds as usual.
--   Do Not Disturb  ⛔ instead of the dots, and no sounds: ~/.tmux/agent-sound.sh reads the same
--                   mode file and stays silent. tmux colours are unaffected. Every remote poll
--                   carries the mode, so the remote agent-sound.sh goes quiet too.
-- The mode and the time it was last switched live in MODE_FILE as "on <epoch>" / "dnd <epoch>", so
-- they survive Hammerspoon reloads and reboots. The file is re-read on every refresh, so
--   echo "dnd $(date +%s)" > ~/.local/state/agent-dots/mode
-- from a shell works too. The item is always in the menu bar.
--
-- Local state is read from tmux: @agent_present / @agent_label (maintained by
-- ~/.tmux/agent-present.sh) and the plugin's per-window @agent-indicator-state. The plugin
-- calculates needs-input > running > done > idle. State drives labels and notifications; title
-- styles drive dot colors. Local and remote polling also detect palette changes without state changes.
-- ~/.cache/agent_dots.state holds the last thing drawn or the last error (debugging without the console).

-- Hammerspoon runs without LANG, and in the C locale tmux replaces tabs and emoji in command output
-- with "_"; force UTF-8 so the tab-separated formats below survive.
local TMUX = "LC_ALL=en_US.UTF-8 /opt/homebrew/bin/tmux -u"
local ITERM = "com.googlecode.iterm2"
local VSCODE = "com.microsoft.VSCode"
local REFRESH = 2 -- seconds, local tmux + clock
local REMOTE_REFRESH = 3 -- seconds, each VS Code host
local REMOTE_TIMEOUT = 20 -- seconds before a hung ssh poll is killed
local MAX_TITLE_WIDTH = 600 -- points, also limited to 30% of the narrowest connected screen
local HOME = os.getenv("HOME")
local STATE_FILE = HOME .. "/.cache/agent_dots.state"
local MODE_DIR = HOME .. "/.local/state/agent-dots"
local MODE_FILE = MODE_DIR .. "/mode" -- same path is hard-coded in ~/.tmux/agent-sound.sh
local SOUND_SCRIPT = HOME .. "/.tmux/agent-sound.sh" -- plays Ping / Glass, honours MODE_FILE
local SSH = "/usr/bin/ssh"
-- No prompts ever (BatchMode); our own multiplexed connection per host, kept 60 s past the last poll;
-- RemoteCommand/RequestTTY override what ~/.ssh/config may set for interactive logins.
local SSH_OPTS = {
    "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
    "-o", "RemoteCommand=none", "-o", "RequestTTY=no", "-o", "LogLevel=ERROR",
    "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-agentdots-%C", "-o", "ControlPersist=60",
}

local MODES = { on = "On", dnd = "Do Not Disturb" }
local STATES = { idle = true, running = true, ["needs-input"] = true, done = true }
local STATE_PRIORITY = { idle = 1, done = 2, running = 3, ["needs-input"] = 4 }
local function highestPriority(windows)
    local top
    for _, w in ipairs(windows) do
        if not top or STATE_PRIORITY[w.state] > STATE_PRIORITY[top.state] then top = w end
    end
    return top
end
-- ANSI colors are theme-dependent. Use stable menu-bar equivalents for named
-- colors; RGB and the extended 256-color palette have exact portable values.
local ANSI = {
    "#000000", "#EF4444", "#22C55E", "#EAB308", "#3B82F6", "#D946EF", "#06B6D4", "#D1D5DB",
    "#6B7280", "#F87171", "#4ADE80", "#FACC15", "#60A5FA", "#E879F9", "#22D3EE", "#FFFFFF",
}
local NAMED = {
    black = 0, red = 1, green = 2, yellow = 3, blue = 4, magenta = 5, cyan = 6, white = 7,
    brightblack = 8, brightred = 9, brightgreen = 10, brightyellow = 11,
    brightblue = 12, brightmagenta = 13, brightcyan = 14, brightwhite = 15,
}
local function colorHex(value)
    value = (value or ""):lower()
    if value:match("^#%x%x%x%x%x%x$") then return value:upper() end
    local index = NAMED[value] or tonumber(value:match("^colou?r(%d+)$"))
    if not index or index > 255 then return nil end
    if index < 16 then return ANSI[index + 1] end
    if index >= 232 then
        local level = 8 + (index - 232) * 10
        return ("#%02X%02X%02X"):format(level, level, level)
    end
    local levels = { 0, 95, 135, 175, 215, 255 }
    index = index - 16
    return ("#%02X%02X%02X"):format(levels[math.floor(index / 36) + 1],
        levels[math.floor(index / 6) % 6 + 1], levels[index % 6 + 1])
end

local function styleColor(style)
    local bg, fg, reverse = nil, nil, false
    for token in (style or ""):gmatch("[^,]+") do
        token = token:match("^%s*(.-)%s*$")
        if token:sub(1, 3) == "bg=" then bg = token:sub(4)
        elseif token:sub(1, 3) == "fg=" then fg = token:sub(4)
        elseif token == "reverse" then reverse = true
        elseif token == "noreverse" then reverse = false end
    end
    if reverse then bg, fg = fg, bg end
    local color = colorHex(bg) and bg or colorHex(fg) and fg or "#9CA3AF"
    return colorHex(color), color
end

-- Apply the same base/current style layering as the window list. An unstyled
-- active title must not inherit a state color that tmux is not displaying.
local WINDOW_STYLE = "#{E:status-style},"
    .. "#{?#{&&:#{window_active},#{!=:#{E:window-status-current-style},default}},#{E:window-status-current-style},#{E:window-status-style}}"
    .. "#{?#{&&:#{window_last_flag},#{!=:#{E:window-status-last-style},default}},#,#{E:window-status-last-style},}"
    .. "#{?#{&&:#{window_bell_flag},#{!=:#{E:window-status-bell-style},default}},#,#{E:window-status-bell-style},"
    .. "#{?#{&&:#{||:#{window_activity_flag},#{window_silence_flag}},#{!=:#{E:window-status-activity-style},default}},#,#{E:window-status-activity-style},}}"
local DOT_FONT = { name = "Menlo", size = 15 }
local GAP_FONT = { name = "Menlo", size = 5 }
local CLOCK_FONT = { name = "Menlo", size = 13 }
local SEP_FONT = { name = "Menlo", size = 13 }
local DND_GLYPH = "⛔\u{FE0F}" -- variation selector: always the colour emoji
local EMPTY_GLYPH = "○" -- a segment with no agent windows
local EMPTY_COLOR = { hex = "#9CA3AF" }
local SEP_COLOR = { hex = "#9CA3AF" }
local CLOCK_COLOR = { hex = "#FFFFFF" }
local LABEL_FONT = { name = "Menlo", size = 13 } -- "T: " / "VS - host - place : " prefixes
local LABEL_COLOR = { hex = "#FFFFFF" }

-- Probe script run on each VS Code host (see its header for the protocol). Sent inline, base64.
local REMOTE_SCRIPT = [==[
# agent-dots remote probe: runs on the ssh host (sent inline by agent_dots.lua, base64 over ssh).
#   poll <on|dnd>              list agent windows of the host tmux and of every VS Code dev container
#                              (tmux found via /tmp/tmux-<uid>), and sync the Do Not Disturb mode there
#   jump <spec> <session> <wid> select that window (spec = "host" or "<container-id>:<uid>")
# Output of poll, tab-separated:
#   T <spec> <name>                                                    one per tmux server found
#   W <spec> <name> <window_id> <index> <label> <active> <session> <state> <style>   one per agent window
set -u
cmd=${1:-poll}; a1=${2:-}; a2=${3:-}; a3=${4:-}
# tmux formats use | as separator: a C locale would turn tabs into "_" in tmux client output
FMT_W='W|#{window_id}|#{window_index}|#{@agent_present}|#{@agent_label}|#{window_active}|#{session_name}|#{@agent-indicator-state}|__WINDOW_STYLE__'

tm() { # tm <spec> <tmux args...>
    local spec=$1; shift
    if [ "$spec" = host ]; then
        LC_ALL=C.UTF-8 tmux -u "$@" 2>/dev/null
    else
        local id=${spec%%:*} uid=${spec#*:}
        docker exec -u "$uid" "$id" env LC_ALL=C.UTF-8 tmux -u -S "/tmp/tmux-$uid/default" "$@" 2>/dev/null
    fi
}

targets() { # lines: <spec>\t<name>
    printf 'host\t%s\n' "$(hostname -s 2>/dev/null || hostname)"
    command -v docker >/dev/null 2>&1 || return 0
    local id name folder d uid
    while IFS='|' read -r id name folder; do
        [ -n "$id" ] || continue
        [ -n "$folder" ] && name=${folder##*/}
        for d in $(docker exec "$id" sh -c 'ls -d /tmp/tmux-* 2>/dev/null' 2>/dev/null); do
            uid=${d#/tmp/tmux-}
            case "$uid" in *[!0-9]*) continue ;; esac
            printf '%s:%s\t%s\n' "$id" "$uid" "$name"
        done
    done < <(docker ps --filter label=devcontainer.local_folder --format '{{.ID}}|{{.Names}}|{{.Label "devcontainer.local_folder"}}' 2>/dev/null)
}

sync_mode() { # sync_mode <spec> <mode>: write "<mode> <epoch>" to ~/.local/state/agent-dots/mode if it differs
    local spec=$1 mode=$2
    local snippet='h=$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6); f="${h:-${HOME:-/tmp}}/.local/state/agent-dots/mode"; [ "$(cut -d" " -f1 "$f" 2>/dev/null)" = "$1" ] && exit 0; mkdir -p "${f%/*}" && echo "$1 $(date +%s)" > "$f"'
    if [ "$spec" = host ]; then
        sh -c "$snippet" sh "$mode" 2>/dev/null
    else
        docker exec -u "${spec#*:}" "${spec%%:*}" sh -c "$snippet" sh "$mode" 2>/dev/null
    fi
}

emit() { # emit <spec> <name>: consume the same window state used by tmux titles
    local spec=$1 name=$2
    tm "$spec" list-windows -a -F "$FMT_W" | awk -F'|' -v OFS='\t' -v spec="$spec" -v name="$name" '
        $1=="W" && $4=="1" {
            state=$8
            if (state != "idle" && state != "running" && state != "needs-input" && state != "done") {
                state="idle"
                n=split($9, style, ",")
                for (i=1; i<=n; i++) {
                    if (style[i]=="bg=magenta") state="needs-input"
                    if (style[i]=="bg=yellow") state="running"
                    if (style[i]=="bg=red") state="done"
                }
            }
            print "W", spec, name, $2, $3, $5, $6, $7, state, $9
        }'
}

case "$cmd" in
    poll)
        while IFS=$'\t' read -r spec name; do
            printf 'T\t%s\t%s\n' "$spec" "$name"
            [ -n "$a1" ] && sync_mode "$spec" "$a1"
            emit "$spec" "$name"
        done < <(targets) ;;
    jump)
        for c in $(tm "$a1" list-clients -F '#{client_name}'); do tm "$a1" switch-client -c "$c" -t "$a2"; done
        tm "$a1" select-window -t "$a3" ;;
    *) echo "usage: poll <on|dnd> | jump <spec> <session> <window_id>" >&2; exit 2 ;;
esac
exit 0
]==]
REMOTE_SCRIPT = REMOTE_SCRIPT:gsub("__WINDOW_STYLE__", function() return WINDOW_STYLE end)

local menubar, dropdown = nil, nil -- one visible item; a separate, hidden item supplies the popup menu
local timer, remoteTimer = nil, nil
local lastKey = nil -- fingerprint of what is currently drawn, to skip redundant redraws
local compactOverride = nil -- start with automatic sizing; arrow clicks override until reload
local compact = false -- layout currently shown
local arrowWidth = 0 -- clickable width at the right edge; zero when DND hides the arrow
local remote = {} -- host -> { windows, states, targets, ok, err, task, started }
local segments = {} -- { kind = "VS" | "SSH", host, window = local ssh window id or nil }, display order
local segmentsKey = "" -- fingerprint of `segments`, to redraw when the list changes
local remoteHostList = {} -- unique hosts in `segments`, sorted
local KIND_NAME = { VS = "VS Code", SSH = "SSH" }
local lastJump = nil -- keeps the most recent jump task referenced until it exits
local REMOTE_B64 = nil
local refresh, setMode -- defined below, referenced by menu()

local function shell(cmd)
    local out, ok = hs.execute(cmd)
    if not ok or not out then return nil end
    return out
end

local function note(line)
    local f = io.open(STATE_FILE, "w")
    if f then
        f:write(os.date("%H:%M:%S "), line, "\n")
        f:close()
    end
    print("[agent_dots] " .. line)
end

local function shq(s) -- single-quote for a POSIX shell
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

-- Mode file: "on <epoch>" / "dnd <epoch>" (epoch = when that mode was switched on).
local function writeMode(mode)
    local now = os.time()
    local f = io.open(MODE_FILE, "w")
    if f then
        f:write(mode, " ", now, "\n")
        f:close()
    end
    return mode, now
end

-- Returns mode, since. A missing or unreadable file means On, starting now (and is written back).
local function readMode()
    local f = io.open(MODE_FILE, "r")
    if f then
        local line = f:read("l") or ""
        f:close()
        local mode, since = line:match("^(%a+)%s+(%d+)")
        if MODES[mode] then return mode, tonumber(since) end
    end
    return writeMode("on")
end

local function clock(since)
    local elapsed = math.max(0, os.time() - since)
    return ("%02d:%02d"):format(elapsed // 3600, (elapsed % 3600) // 60)
end

-- Same sounds as a local agent; the script itself stays silent in Do Not Disturb.
local function play(state)
    hs.execute(("AGENT_STATE=%s %s"):format(state, shq(SOUND_SCRIPT)))
end

---------------------------------------------------------------------------------------------------
-- Local tmux
---------------------------------------------------------------------------------------------------

-- Windows with an agent, in index order: { id, index, label, active, state }. nil if tmux is down.
local function agentWindows()
    local out = shell(TMUX .. " list-windows -a -F '#{window_id}\t#{window_index}\t#{@agent_present}\t#{@agent_label}\t#{window_active}\t#{@agent-indicator-state}\t" .. WINDOW_STYLE .. "'")
    if not out then return nil end

    local windows = {}
    for line in out:gmatch("[^\r\n]+") do
        local id, index, present, label, active, state, style = line:match("^([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)$")
        if id and present == "1" then
            local w = { id = id, index = tonumber(index) or 0, label = label, active = (active == "1"), state = STATES[state] and state or "idle", color = styleColor(style) }
            table.insert(windows, w)
        end
    end

    table.sort(windows, function(a, b) return a.index < b.index end)
    if #windows == 0 then
        -- Debug aid: keep what tmux actually returned when nothing qualified.
        local f = io.open(HOME .. "/.cache/agent_dots.raw", "w")
        if f then
            f:write(os.date("%H:%M:%S"), " raw list-windows output:\n", (out:gsub("\t", "<TAB>")), "\n")
            f:close()
        end
    end
    return windows
end

local function jumpTo(w)
    return function()
        shell(TMUX .. " select-window -t " .. w.id)
        hs.application.launchOrFocusByBundleID(ITERM)
    end
end

-- SSH windows and their published colors, including disconnected windows needing cleanup.
local function sshLinks()
    local out = shell(TMUX .. " list-windows -a -F '#{window_id}\t#{window_index}\t#{@ssh_host}\t#{@ssh_agent_host}\t#{@ssh_agent_color}'") or ""
    local links, seen = {}, {}
    for line in out:gmatch("[^\r\n]+") do
        local id, _, host, colorHost, color = line:match("^([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)\t([^\t]*)$")
        if id and id:match("^@%d+$") and (host ~= "" or colorHost ~= "" or color ~= "") and not seen[id] then
            seen[id] = true
            links[#links + 1] = { host = host, window = id, colorHost = colorHost, color = color }
        end
    end
    return links
end

---------------------------------------------------------------------------------------------------
-- VS Code hosts (Remote-SSH)
---------------------------------------------------------------------------------------------------

-- Hosts VS Code is connected to: the last word of every `ssh …` process whose parent is VS Code's
-- Remote-SSH extension host (e.g. "ssh -v -T -D 63706 -o ConnectTimeout=60 -o RemoteCommand=none colossus").
local function vscodeHosts()
    local out = shell("/bin/ps -axo pid=,ppid=,command=") or ""
    local cmdByPid, rows = {}, {}
    for line in out:gmatch("[^\r\n]+") do
        local pid, ppid, cmd = line:match("^%s*(%d+)%s+(%d+)%s+(.-)%s*$")
        if pid then
            cmdByPid[pid] = cmd
            rows[#rows + 1] = { ppid = ppid, cmd = cmd }
        end
    end
    local hosts, seen = {}, {}
    for _, r in ipairs(rows) do
        if r.cmd:match("^%S*/?ssh ") then
            local parent = cmdByPid[r.ppid] or ""
            if parent:find("remote%-ssh") then
                local host = r.cmd:match("(%S+)$")
                if host and not seen[host] then
                    seen[host] = true
                    hosts[#hosts + 1] = host
                end
            end
        end
    end
    table.sort(hosts)
    return hosts
end

local function sshArgs(host, remoteArgs)
    local args = {}
    for _, o in ipairs(SSH_OPTS) do args[#args + 1] = o end
    args[#args + 1] = host
    args[#args + 1] = ("echo %s | base64 -d | bash -s -- %s"):format(REMOTE_B64, remoteArgs)
    return args
end

-- A shared host poll includes dev containers for VS Code. Direct SSH represents only the host.
local function belongsToSegment(seg, w)
    return seg.kind ~= "SSH" or w.spec == "host"
end

local function windowsForSegment(seg)
    local windows = {}
    local r = remote[seg.host]
    for _, w in ipairs(r and r.ok and r.windows or {}) do
        if belongsToSegment(seg, w) then windows[#windows + 1] = w end
    end
    return windows
end

-- Publish only display options; local pane states and the plugin's style bookkeeping stay independent.
local function syncSshColors()
    local commands = {}
    for _, link in ipairs(sshLinks()) do
        local r = remote[link.host]
        local top = highestPriority(windowsForSegment({ kind = "SSH", host = link.host }))
        local color = r and r.ok and (top and top.tmuxColor or "green") or ""
        if link.colorHost ~= link.host or link.color ~= color then
            commands[#commands + 1] = "set-option -wq -t " .. link.window .. " @ssh_agent_host " .. shq(link.host)
            commands[#commands + 1] = "set-option -wq -t " .. link.window .. " @ssh_agent_color " .. shq(color)
        end
    end
    if #commands > 0 then
        shell(TMUX .. " " .. table.concat(commands, " \\; "))
    end
end

local function windowIsVisible(host, w)
    for _, seg in ipairs(segments) do
        if seg.host == host and belongsToSegment(seg, w) then return true end
    end
    return false
end

-- Parse one poll result into remote[host]; play a sound for windows that changed to needs-input / done
-- (never on the first successful poll of a host, which only sets the baseline).
local function applyPoll(host, code, out, err)
    local r = remote[host]
    if not r then return end
    if code ~= 0 then
        r.ok, r.windows, r.targets = false, {}, 0
        r.err = ("ssh exit %s %s"):format(tostring(code), (err or ""):gsub("%s+", " ")):sub(1, 100)
        return
    end
    local windows, targets, states = {}, 0, {}
    for line in (out or ""):gmatch("[^\r\n]+") do
        local f = {}
        for field in (line .. "\t"):gmatch("([^\t]*)\t") do f[#f + 1] = field end
        if f[1] == "T" then
            targets = targets + 1
        elseif f[1] == "W" and #f >= 9 then
            local color, tmuxColor = styleColor(f[10])
            local w = { spec = f[2], where = f[3], id = f[4], index = tonumber(f[5]) or 0, label = f[6],
                        active = (f[7] == "1"), session = f[8], state = STATES[f[9]] and f[9] or "idle",
                        color = color, tmuxColor = tmuxColor }
            windows[#windows + 1] = w
            local key = w.spec .. "/" .. w.id
            states[key] = w.state
            local prev = r.states[key]
            if r.ok and prev and prev ~= w.state and (w.state == "needs-input" or w.state == "done")
                and windowIsVisible(host, w) then
                play(w.state)
            end
        end
    end
    table.sort(windows, function(a, b)
        if a.where ~= b.where then return a.where < b.where end
        return a.index < b.index
    end)
    r.windows, r.targets, r.states, r.ok, r.err = windows, targets, states, true, nil
end

-- One ssh poll per host at a time; the current mode rides along so the remote mode file follows ours.
local function pollRemote(host)
    local r = remote[host]
    if r.task then
        if r.task:isRunning() then
            if os.time() - (r.started or 0) < REMOTE_TIMEOUT then return end
            r.task:terminate()
            r.ok, r.err = false, "timeout"
        end
        r.task = nil
    end
    local mode = readMode()
    local task = hs.task.new(SSH, function(code, out, err)
        r.task = nil
        local ok, e = pcall(applyPoll, host, code, out, err)
        if not ok then note("remote " .. host .. " error: " .. tostring(e)) end
        refresh()
    end, sshArgs(host, "poll " .. mode))
    if task and task:start() then
        r.task, r.started = task, os.time()
    else
        r.ok, r.err = false, "cannot start ssh"
    end
end

-- Rebuild the segment list from both sources and poll every distinct host once.
local function pollAll()
    local segs, hosts, seen, key = {}, {}, {}, {}
    for _, h in ipairs(vscodeHosts()) do segs[#segs + 1] = { kind = "VS", host = h } end
    local sshHosts = {}
    for _, l in ipairs(sshLinks()) do
        if l.host ~= "" and not sshHosts[l.host] then
            sshHosts[l.host] = true
            segs[#segs + 1] = { kind = "SSH", host = l.host, window = l.window }
        end
    end
    for _, s in ipairs(segs) do
        key[#key + 1] = s.kind .. ":" .. s.host
        if not seen[s.host] then
            seen[s.host] = true
            hosts[#hosts + 1] = s.host
            if not remote[s.host] then remote[s.host] = { windows = {}, states = {}, targets = 0, ok = false } end
        end
    end
    table.sort(hosts)
    key = table.concat(key, ",")
    for h, r in pairs(remote) do
        if not seen[h] then
            if r.task and r.task:isRunning() then r.task:terminate() end
            remote[h] = nil
        end
    end
    local changed = (key ~= segmentsKey)
    segments, remoteHostList, segmentsKey = segs, hosts, key -- before polling: a poll callback redraws
    for _, h in ipairs(hosts) do pollRemote(h) end
    if changed then refresh() end
end

-- Select the window in its remote tmux, then bring the matching app forward: VS Code for a VS
-- segment, iTerm (on the ssh window) for an SSH segment.
local function jumpRemote(seg, w)
    return function()
        local t = hs.task.new(SSH, nil, sshArgs(seg.host, ("jump %s %s %s"):format(w.spec, shq(w.session), w.id)))
        if t and t:start() then lastJump = t end
        if seg.kind == "SSH" then
            if seg.window then shell(TMUX .. " select-window -t " .. seg.window) end
            hs.application.launchOrFocusByBundleID(ITERM)
        else
            hs.application.launchOrFocusByBundleID(VSCODE)
        end
    end
end

---------------------------------------------------------------------------------------------------
-- Drawing
---------------------------------------------------------------------------------------------------

local function dots(windows)
    local text = nil
    for _, w in ipairs(windows) do
        local dot = hs.styledtext.new(w.active and "◉" or "●", { color = { hex = w.color }, font = DOT_FONT })
        text = text and (text .. hs.styledtext.new(" ", { font = GAP_FONT }) .. dot) or dot
    end
    return text
end

local function segment(windows)
    if #windows == 0 then return hs.styledtext.new(EMPTY_GLYPH, { color = EMPTY_COLOR, font = DOT_FONT }) end
    return dots(windows)
end

local function label(s) return hs.styledtext.new(s, { color = LABEL_COLOR, font = LABEL_FONT }) end
local function sep() return hs.styledtext.new("  |  ", { color = SEP_COLOR, font = SEP_FONT }) end

-- Counts represent agent windows, with the color of the highest-priority window.
local function summary(windows)
    if #windows == 0 then return segment(windows) end
    local top = highestPriority(windows)
    return hs.styledtext.new("●", { color = { hex = top.color }, font = DOT_FONT }) .. label(tostring(#windows))
end

-- Remote windows grouped by place (the host's tmux, or one dev container), keeping their sorted order.
local function byPlace(windows)
    local groups, index = {}, {}
    for _, w in ipairs(windows) do
        local g = index[w.spec]
        if not g then
            g = { spec = w.spec, where = w.where, windows = {} }
            index[w.spec] = g
            groups[#groups + 1] = g
        end
        g.windows[#g.windows + 1] = w
    end
    return groups
end

-- "VS - host" / "SSH - host" for the host's own tmux, "… - container" for a dev container.
local function segmentName(seg, g, kindName)
    local name = (kindName or seg.kind) .. " - " .. seg.host
    if g and g.spec ~= "host" then name = name .. " - " .. g.where end
    return name
end

-- At most two remote summaries, regardless of the number or length of host/container names.
local function remoteSummary()
    local groups, order = {}, {}
    for _, seg in ipairs(segments) do
        local g = groups[seg.kind]
        if not g then
            g = { windows = {}, seen = {} }
            groups[seg.kind] = g
            order[#order + 1] = seg.kind
        end
        for _, w in ipairs(windowsForSegment(seg)) do
            local key = seg.host .. "/" .. w.spec .. "/" .. w.id
            if not g.seen[key] then
                g.seen[key] = true
                g.windows[#g.windows + 1] = w
            end
        end
    end
    local text = label("")
    for _, kind in ipairs(order) do
        local g = groups[kind]
        text = text .. sep() .. label(kind .. ": ") .. summary(g.windows)
    end
    return text
end

local function titleWidthLimit()
    local width = MAX_TITLE_WIDTH
    for _, screen in ipairs(hs.screen.allScreens()) do
        width = math.min(width, screen:fullFrame().w * 0.30)
    end
    return math.floor(width)
end

-- Menu bar title: "T: <dots>" for the local tmux, then one "<segment name> : <dots>" per remote
-- place with agent windows ("… : ○" when a host has none), or ⛔ alone; then the stopwatch and arrow.
local function title(mode, windows, since, collapsed)
    local text
    if mode == "dnd" then
        text = hs.styledtext.new(DND_GLYPH, { font = DOT_FONT })
    else
        text = label("T: ") .. (collapsed and summary(windows) or segment(windows))
        if collapsed then
            text = text .. remoteSummary()
        else
            for _, seg in ipairs(segments) do
                local groups = byPlace(windowsForSegment(seg))
                if #groups == 0 then
                    text = text .. sep() .. label(segmentName(seg) .. " : ") .. segment({})
                end
                for _, g in ipairs(groups) do
                    text = text .. sep() .. label(segmentName(seg, g) .. " : ") .. segment(g.windows)
                end
            end
        end
    end
    text = text .. hs.styledtext.new("  " .. clock(since), { color = CLOCK_COLOR, font = CLOCK_FONT })
    if mode ~= "dnd" then text = text .. label(collapsed and "  >" or "  <") end
    return text
end

-- Dropdown line "<text> — ● <state>" with the dot in the state colour. The plain runs get the menu
-- font and the menu's text colour explicitly (white in dark mode, black otherwise), because runs
-- without attributes would not follow the appearance.
local function menuTextAttrs()
    local ok, style = pcall(function() return hs.host.interfaceStyle() end)
    local font = (hs.styledtext.defaultFonts or {}).menu or { name = ".AppleSystemUIFont", size = 13 }
    return { font = font, color = { hex = (ok and style == "Dark") and "#FFFFFF" or "#000000" } }
end

local function entry(text, w, attrs)
    return hs.styledtext.new(text .. " — ", attrs)
        .. hs.styledtext.new("●", { color = { hex = w.color }, font = attrs.font })
        .. hs.styledtext.new(" " .. w.state, attrs)
end

local function windowsKey(windows)
    local parts = {}
    for _, w in ipairs(windows) do
        parts[#parts + 1] = (w.spec or "") .. ":" .. (w.where or "") .. ":" .. w.id
            .. ":" .. w.state .. ":" .. w.color .. (w.active and "*" or "")
    end
    return table.concat(parts, ",")
end

-- Built on click, so it always reflects the current state.
local function menu()
    local mode = readMode()
    local items = {
        { title = MODES.on, checked = (mode == "on"), fn = function() setMode("on") end },
        { title = MODES.dnd, checked = (mode == "dnd"), fn = function() setMode("dnd") end },
        { title = "-" },
    }
    local attrs = menuTextAttrs()
    local windows = agentWindows()
    for _, w in ipairs(windows or {}) do
        table.insert(items, { title = entry(("%d: %s"):format(w.index, w.label), w, attrs), fn = jumpTo(w) })
    end
    if not windows then
        table.insert(items, { title = "tmux is not running", disabled = true })
    elseif #windows == 0 then
        table.insert(items, { title = "no agent windows", disabled = true })
    end
    -- One header per remote place, same wording as the menu bar: "VS Code - <host> - <place>" / "SSH - <host>".
    for _, seg in ipairs(segments) do
        local r = remote[seg.host]
        local groups = byPlace(windowsForSegment(seg))
        table.insert(items, { title = "-" })
        if #groups == 0 then
            local header = segmentName(seg, nil, KIND_NAME[seg.kind])
            if r.err then
                header = header .. " — " .. r.err
            elseif not r.ok then
                header = header .. " — connecting…"
            elseif r.targets == 0 then
                header = header .. " — no tmux found"
            else
                header = header .. " — no agent windows"
            end
            table.insert(items, { title = header, disabled = true })
        end
        for _, g in ipairs(groups) do
            table.insert(items, { title = segmentName(seg, g, KIND_NAME[seg.kind]), disabled = true })
            for _, w in ipairs(g.windows) do
                table.insert(items, { title = entry(("%d: %s"):format(w.index, w.label), w, attrs),
                                      fn = jumpRemote(seg, w), indent = 1 })
            end
        end
    end
    return items
end

-- An attached menu swallows the item's click callback. Keep it on a hidden item instead,
-- so the visible title can distinguish the trailing arrow from the dots and clock.
local function clicked()
    local point = hs.mouse.absolutePosition()
    local frame = menubar:frame()
    if arrowWidth > 0 and frame and point.x >= frame.x + frame.w - arrowWidth
        and point.x <= frame.x + frame.w then
        compactOverride = not compact
        refresh()
    else
        dropdown:setMenu(menu())
        dropdown:popupMenu(point)
    end
end

local function draw()
    syncSshColors()
    local windows = agentWindows() or {} -- tmux down: still show the mode and the clock
    local mode, since = readMode()

    local widthLimit = titleWidthLimit()
    local key = { mode, clock(since), tostring(compactOverride), tostring(widthLimit), "L=" .. windowsKey(windows) }
    for _, seg in ipairs(segments) do
        local r = remote[seg.host]
        key[#key + 1] = seg.kind .. ":" .. seg.host .. "=" .. (r.ok and windowsKey(windowsForSegment(seg)) or ("err:" .. tostring(r.err)))
    end
    key = table.concat(key, " ")
    if key == lastKey then return end
    local text = title(mode, windows, since, false)
    compact = mode ~= "dnd" and (compactOverride == true
        or (compactOverride == nil and hs.drawing.getTextDrawingSize(text).w + 16 > widthLimit))
    if compact then text = title(mode, windows, since, true) end
    -- Include the space before the arrow and the native status item's right inset.
    arrowWidth = mode == "dnd" and 0 or hs.drawing.getTextDrawingSize(label(" >")).w + 8
    if not menubar:isInMenuBar() then menubar:returnToMenuBar() end
    menubar:setTitle(text)
    lastKey = key
    note("drawn " .. (compact and "compact " or "expanded ") .. key)
end

refresh = function()
    local ok, err = pcall(draw)
    if not ok then note("error: " .. tostring(err)) end
end

-- Switch mode; the stopwatch restarts only when the mode actually changes. The remotes pick the new
-- mode up with their next poll (forced right away).
setMode = function(mode)
    if not MODES[mode] then return end
    local current = readMode()
    if current ~= mode then
        writeMode(mode)
        lastKey = nil
        pcall(pollAll)
    end
    refresh()
end

os.execute("mkdir -p " .. HOME .. "/.cache " .. MODE_DIR)
local ok, err = pcall(function()
    REMOTE_B64 = hs.base64.encode(REMOTE_SCRIPT):gsub("%s+", "")
    menubar = hs.menubar.new()
    dropdown = hs.menubar.new(false)
    menubar:setClickCallback(clicked)
end)
if ok then
    note("loaded")
    refresh()
    pcall(pollAll)
    timer = hs.timer.doEvery(REFRESH, refresh)
    remoteTimer = hs.timer.doEvery(REMOTE_REFRESH, function() pcall(pollAll) end)
else
    note("error creating menubar: " .. tostring(err))
end

-- Returned so `require` keeps the timers and menubar referenced (Hammerspoon garbage-collects
-- unreferenced timers). The rest is exposed for the offline harness.
return { menubar = menubar, dropdown = dropdown, timer = timer, remoteTimer = remoteTimer, refresh = refresh, agentWindows = agentWindows,
         styleColor = styleColor, menu = menu, setMode = setMode, readMode = readMode, vscodeHosts = vscodeHosts, pollAll = pollAll,
         applyPoll = applyPoll, jumpRemote = jumpRemote, remote = remote, sshLinks = sshLinks,
         remoteHosts = function() return remoteHostList end, segments = function() return segments end }
