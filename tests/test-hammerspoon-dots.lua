local originalGetenv = os.getenv
os.getenv = function(key) return key == "HOME" and arg[2] or originalGetenv(key) end
local vscode = true
local localOutput = ''
local localCommand = nil
local remoteScript = nil
local renders = 0
local drawnColors = {}
local menuColors = {}
local sounds = 0
local shown = ''
local screenWidths = {5000}
local screenOrigin = 0
local selectedWindow, focusedApp = nil, nil
local sshOutput = '@6\t5\tcolossus\n'
local sshOptions = {}
local sshWrites = 0
local sshCommand
local mousePoint = {x = 0, y = 0}
local popupCount = 0
local popupItems = nil
local tasks, startTasks = {}, false
local styled = {}
local function asStyled(value)
    if type(value) == 'table' then return value end
    return {text = value, colors = {}}
end
styled.__concat = function(a, b)
    a, b = asStyled(a), asStyled(b)
    local colors = {}
    for _, color in ipairs(a.colors) do colors[#colors+1] = color end
    for _, color in ipairs(b.colors) do colors[#colors+1] = color end
    return setmetatable({text = a.text .. b.text, colors = colors, height = math.max(a.height or 16, b.height or 16)}, styled)
end
styled.__index = {find = function(self, ...) return self.text:find(...) end}
local hotkeyFn = nil
local expansionFn = nil
local activeHotkeys = {}
hs = {
    hotkey = {
        new = function(mods, key, fn)
            return {idx = table.concat(mods, '+') .. ':' .. key,
                enable = function(self)
                    activeHotkeys[#activeHotkeys+1] = self
                    if key == 'F9' and #mods == 0 then expansionFn = fn else hotkeyFn = fn end
                    return self
                end,
                delete = function(self) self.deleted = true end,
            }
        end,
        getHotkeys = function() return activeHotkeys end,
        systemAssigned = function(mods, key) return key == 'F11' and {enabled = true} or nil end,
    },
    execute = function(command)
        if command:find('select-window',1,true) then
            selectedWindow = command
            return '', true
        elseif command:find('set-option -wq',1,true) then
            sshWrites = sshWrites + 1
            sshCommand = command
            assert(not command:find('window-status',1,true), 'remote colors must not overwrite local agent styles')
            for id, option, value in command:gmatch("set%-option %-wq %-t (@%d+) (@ssh_agent_%w+) '([^']*)'") do
                sshOptions[id] = sshOptions[id] or {}
                sshOptions[id][option] = value
            end
            return '', true
        elseif command:find('/bin/ps',1,true) then
            return vscode and '10 1 Code remote-ssh\n11 10 /usr/bin/ssh colossus\n' or '', true
        elseif command:find('@ssh_host',1,true) then
            local rows = {}
            for row in sshOutput:gmatch('[^\n]+') do
                local id = row:match('^([^\t]+)')
                local options = sshOptions[id] or {}
                rows[#rows + 1] = row .. '\t' .. (options['@ssh_agent_host'] or '')
                    .. '\t' .. (options['@ssh_agent_color'] or '')
            end
            return table.concat(rows, '\n'), true
        elseif command:find('@agent-indicator-state',1,true) then
            localCommand = command
            return localOutput, true
        elseif command:find('AGENT_STATE=',1,true) then
            sounds = sounds + 1
            return '', true
        end
        error('Unexpected command: '..command)
    end,
    base64 = {encode = function(script) remoteScript=script; return '' end},
    styledtext = {new = function(text, attrs)
        local colors = {}
        if (text == '●' or text == '◉') and attrs.color then
            menuColors[#menuColors+1] = attrs.color.hex
            colors[1] = attrs.color.hex
        end
        return setmetatable({text = text, colors = colors, height = (attrs.font and attrs.font.size or 13) + 3}, styled)
    end},
    drawing = {getTextDrawingSize = function(text) return {w = utf8.len(text.text) * 4, h = text.height or 16} end},
    screen = {allScreens = function()
        local screens = {}
        local offset = screenOrigin
        for _, width in ipairs(screenWidths) do
            local left = offset
            screens[#screens+1] = {
                fullFrame = function() return {x = left, y = 0, w = width, h = 1000} end,
                frame = function() return {x = left, y = 24, w = width, h = 976} end,
            }
            offset = offset + width
        end
        return screens
    end},
    application = {launchOrFocusByBundleID = function(bundle) focusedApp = bundle end},
    canvas = {new = function(frame) return {
        bounds = frame,
        level = function(self) return self end,
        behavior = function(self) return self end,
        clickActivating = function(self, value) assert(value == false); return self end,
        mouseCallback = function(self, callback) self.click = callback; return self end,
        frame = function(self, value) if value then self.bounds = value; return self end; return self.bounds end,
        replaceElements = function(self, elements) self.elements = elements; return self end,
        show = function(self) self.visible = true; return self end,
        delete = function(self) self.visible = false; self.deleted = true end,
    } end},
    mouse = {absolutePosition = function() return mousePoint end},
    menubar = {new = function(visible) return {
        setMenu = function(self, items) self.items = items end,
        setClickCallback = function(self, callback) self.click = callback end,
        popupMenu = function(self, point)
            assert(visible == false, 'popup must not replace the visible item or intercept its callback')
            assert(point == mousePoint, 'menu must open at the clicked location')
            popupCount = popupCount + 1
            popupItems = self.items
        end,
        frame = function() return {x = 100, y = 0, w = utf8.len(shown) * 4 + 16, h = 24} end,
        isInMenuBar = function() return visible ~= false end,
        setTitle = function(_,text) shown=text.text; renders=renders+1; drawnColors=text.colors; menuColors={} end,
    } end},
    timer = {doEvery = function() return {} end},
    task = {new = function(executable, callback, args)
        assert(executable:match('/scripts/vpn%-guard%.py$'), 'remote tasks must use the VPN guard')
        assert(args[1] == '--probe' and args[2] == 'colossus', 'probes must check their destination without waiting')
        local options = {}
        for _, value in ipairs(args) do
            local key, setting = value:match('^(%w+)=(.*)$')
            if key then options[key] = setting end
        end
        assert(options.ControlMaster == 'no' and options.ControlPath == 'none'
            and options.ControlPersist == 'no' and options.ForkAfterAuthentication == 'no',
            'background SSH masters can retain task pipes and freeze Hammerspoon')
        local task = {callback = callback, args = args,
            start = function(self) self.running = startTasks; return startTasks end,
            isRunning = function(self) return self.running end,
            terminate = function(self) self.running = false; self.terminated = true end}
        tasks[#tasks + 1] = task
        return task
    end},
    host = {interfaceStyle=function() return 'Dark' end},
}
hs.screen.mainScreen = function() return hs.screen.allScreens()[1] end
AGENT_DOTS = { hotkey = { mods = {'cmd'}, key = 'F9' } } -- init.lua-style override
local dots = dofile(arg[1])
assert(remoteScript:find('#{E:window-status-current-style}',1,true), 'remote poll must read actual title styles')
assert(not remoteScript:find('__WINDOW_STYLE__',1,true), 'remote probe style placeholder was not expanded')
local fixtures = {
    {'bg=cyan,fg=black', '#06B6D4'},
    {'bg=colour208', '#FF8700'},
    {'bg=color196', '#FF0000'},
    {'bg=colour232', '#080808'},
    {'bg=colour255', '#EEEEEE'},
    {'bg=brightcyan', '#22D3EE'},
    {'bg=#ab12ef,fg=black', '#AB12EF'},
    {'bg=blue,fg=white,bg=cyan', '#06B6D4'},
    {'bg=blue,fg=red,reverse', '#EF4444'},
    {'bg=blue,fg=red,reverse,noreverse', '#3B82F6'},
    {'bg=default,fg=default', '#9CA3AF'},
    {'bg=colour999', '#9CA3AF'},
}
for _, fixture in ipairs(fixtures) do
    assert(dots.styleColor(fixture[1]) == fixture[2], fixture[1])
end
localOutput = '@1\t1\t1\tcodex\t0\trunning\tbg=yellow,fg=black\n'
dots.refresh()
assert(dots.agentWindows()[1].color == '#EAB308')
assert(localCommand:find('#{E:window-status-current-style}',1,true), 'local poll must read title styles')
local beforeRenders = renders
localOutput = '@1\t1\t1\tcodex\t0\trunning\tbg=#123abc,fg=black\n'
dots.refresh()
assert(renders == beforeRenders + 1, 'palette change without state change must redraw')
assert(drawnColors[1] == '#123ABC', 'local dot must use custom title color')
menuColors = {}
dots.menu()
assert(menuColors[1] == '#123ABC', 'menu dot must use the same custom color')
localOutput = ''
print('PASS: named, indexed, RGB, inherited, and reversed title colors drive local dots and menus')
assert(#dots.segments()==2,'both VS and SSH must be detected')
local function poll(containerState,hostState,style,hostStyle,containerName)
    style = style or 'bg=magenta,fg=black'
    local text = 'T\thost\tcolossus\nT\tcontainer:1000\tkernelcatcher\n'
    for i=0,2 do
        text=text..('W\tcontainer:1000\t%s\t@%d\t%d\tagent\t0\tmain\t%s\t%s\n'):format(containerName or 'kernelcatcher',i,i+1,containerState,style)
    end
    if hostState then text=text..'W\thost\tcolossus\t@0\t1\thost-agent\t0\tmain\t'..hostState..'\t'..(hostStyle or style)..'\n' end
    dots.applyPoll('colossus',0,text,'')
    dots.refresh()
end
poll('idle')
assert(shown:find('SSH - colossus : ○',1,true),shown)
assert(shown:find('VS - colossus - kernelcatcher : ● ● ●',1,true),shown)
assert(not shown:find('SSH - colossus - kernelcatcher',1,true),shown)
local sshSection=false
for _, item in ipairs(dots.menu()) do
    if type(item.title)=='string' and item.title:find('SSH - colossus',1,true) then
        sshSection=true
        assert(item.title:find('no agent windows',1,true),item.title)
    end
    if sshSection then assert(item.indent~=1,'container leaked into SSH menu') end
end
print('PASS: containers appear only under VS, and empty SSH host shows an empty dot and menu')
poll('needs-input')
assert(sounds==3,'visible container notifications should still work')
vscode=false
dots.pollAll()
poll('running')
local before=sounds
poll('done')
assert(sounds==before,'SSH-only connection must not notify for invisible containers')
assert(shown:find('SSH - colossus : ○',1,true),shown)
assert(not shown:find('kernelcatcher',1,true),shown)
poll('done','running')
assert(shown:find('SSH - colossus : ●',1,true),shown)
local hostEntries=0
for _, item in ipairs(dots.menu()) do
    if item.indent==1 then
        hostEntries=hostEntries+1
        assert(item.title:find('host-agent',1,true),'non-host menu entry')
    end
end
assert(hostEntries==1,'SSH should have exactly one host agent entry')
before=sounds
poll('needs-input','needs-input')
assert(sounds==before+1,'SSH host notification should work without container duplicates')
print('PASS: host agents still display and notify, while SSH-only container changes stay hidden')

local beforeRenders = renders
local beforeSounds = sounds
poll('needs-input','needs-input','bg=colour208,fg=black')
assert(renders == beforeRenders + 1, 'remote palette-only changes must redraw')
assert(sounds == beforeSounds, 'palette changes must not trigger state notifications')
assert(drawnColors[#drawnColors] == '#FF8700', 'remote dot did not adopt custom color')
menuColors = {}
dots.menu()
assert(menuColors[1] == '#FF8700', 'remote menu dot did not adopt custom color')
print('PASS: remote palette changes update dots and menus without false state notifications')

vscode = true
dots.pollAll()
poll('running', 'needs-input', 'bg=yellow', 'bg=magenta')
assert(not shown:find('VS - colossus :',1,true) and shown:find('SSH - colossus :',1,true),
    'a shared host must be shown under SSH only')
assert(shown:find('VS - colossus - kernelcatcher',1,true), 'VS Code containers must stay visible')
local function hostMenuEntry()
    local entry, count = nil, 0
    for _, item in ipairs(dots.menu()) do
        if item.indent == 1 and item.title:find('host-agent',1,true) then entry, count = item, count + 1 end
    end
    assert(count == 1, 'a shared host agent must appear exactly once in the dropdown')
    return entry
end
hostMenuEntry().fn()
assert(focusedApp == 'com.googlecode.iterm2' and selectedWindow:find('select-window -t @6',1,true),
    'shared host menu entry must select its SSH window')
sshOutput = ''
dots.pollAll()
poll('running', 'needs-input', 'bg=yellow', 'bg=magenta')
assert(shown:find('VS - colossus :',1,true) and not shown:find('SSH - colossus',1,true),
    'disconnecting SSH must return the host to VS Code')
hostMenuEntry().fn()
assert(focusedApp == 'com.microsoft.VSCode', 'VS-only host must focus VS Code')
sshOutput = '@6\t5\tcolossus\n'
dots.pollAll()
dots.applyPoll('colossus',0,'T\thost\tcolossus\nW\thost\tcolossus\t@0\t1\thost-agent\t0\tmain\tidle\tbg=blue\n','')
dots.refresh()
assert(not shown:find('VS - colossus',1,true), 'an empty container-only source must not duplicate the host label')
hostMenuEntry()
dots.applyPoll('colossus',0,'T\thost\tcolossus\n','')
dots.refresh()
assert(shown:find('SSH - colossus : ○',1,true) and not shown:find('VS - colossus',1,true),
    'an empty shared host needs only one hollow dot')
print('PASS: shared hosts appear once, retain VS containers, and move back to VS Code after SSH disconnects')

-- VS Code can run tmux directly on the host, without a devcontainer.
local splitSources = 'T\thost\tcolossus\n'
    .. 'W\thost\tcolossus\t@6\t0\tterminal-agent\t1\tmain\tidle\tbg=blue\tSSH\n'
    .. 'W\thost\tcolossus\t@8\t0\teditor-agent\t1\tproject\tneeds-input\tbg=magenta\tVS\n'
dots.applyPoll('colossus', 0, splitSources, '')
dots.refresh()
assert(shown:find('VS - colossus - project : ◉',1,true) and shown:find('SSH - colossus : ◉',1,true), shown)
assert(sshOptions['@6']['@ssh_agent_color'] == 'blue', 'a waiting VS Code host session must not color SSH')
local source, counts = nil, {VS = 0, SSH = 0}
for _, item in ipairs(dots.menu()) do
    if type(item.title) == 'string' then
        if item.title:find('VS Code - colossus',1,true) then source = 'VS' end
        if item.title:find('SSH - colossus',1,true) then source = 'SSH' end
    end
    if item.indent == 1 then
        counts[source] = counts[source] + 1
        assert(item.title:find(source == 'VS' and 'editor-agent' or 'terminal-agent',1,true), 'wrong host session source')
        item.fn()
        assert(focusedApp == (source == 'VS' and 'com.microsoft.VSCode' or 'com.googlecode.iterm2'))
    end
end
assert(counts.VS == 1 and counts.SSH == 1, 'each host session must appear once under its source')
local projects = splitSources:gsub('\tproject\t', '\tvscode-bionemo-framework\t')
    .. 'W\thost\tcolossus\t@9\t1\tsecond-editor\t0\tother-project\tidle\tbg=blue\tVS\n'
dots.applyPoll('colossus', 0, projects, '')
dots.refresh()
assert(shown:find('VS - colossus - bionemo-framework : ◉',1,true), shown)
assert(shown:find('VS - colossus - other-project : ●',1,true), 'host projects need separate groups')
local projectMenu = false
for _, item in ipairs(dots.menu()) do
    if item.title == 'VS Code - colossus - bionemo-framework' then projectMenu = true end
end
assert(projectMenu, 'the dropdown must include the project name')
local renameRenders = renders
dots.applyPoll('colossus', 0, projects:gsub('vscode%-bionemo%-framework', 'vscode-renamed-project'), '')
dots.refresh()
assert(renders == renameRenders + 1 and shown:find('VS - colossus - renamed-project',1,true),
    'renaming a session must update its label without a state change')
vscode = false
dots.pollAll()
dots.applyPoll('colossus', 0, splitSources, '')
dots.refresh()
assert(not shown:find('VS -',1,true) and #drawnColors == 1, 'VS host sessions must not leak into an SSH-only view')
dots.applyPoll('colossus', 0, splitSources:gsub('editor%-agent\t1\tproject\tneeds%-input', 'editor-agent\t1\tproject\trunning'), '')
beforeSounds = sounds
dots.applyPoll('colossus', 0, splitSources, '')
assert(sounds == beforeSounds, 'hidden VS host sessions must not notify through SSH')
vscode = true
dots.pollAll()
dots.applyPoll('colossus', 0, splitSources:gsub('\tVS\n', '\tSSH\n'), '')
dots.refresh()
assert(not shown:find('VS - colossus',1,true) and shown:find('SSH - colossus : ◉ ◉',1,true),
    'client source changes must update grouping without a state change')
print('PASS: host VS Code sessions retain their source in dots, menus, colors, and notifications')

local function clickFromRight(distance)
    local frame = dots.menubar:frame()
    mousePoint = {x = frame.x + frame.w - distance, y = frame.y + frame.h / 2}
    assert(not dots.menubar.items, 'attaching a menu prevents direct arrow clicks')
    dots.menubar.click()
end
localOutput = '@1\t1\t1\tcodex\t1\trunning\tbg=yellow\n'
    .. '@2\t2\t1\tclaude\t0\tdone\tbg=red\n'
    .. '@3\t3\t1\tclaude\t0\tneeds-input\tbg=magenta\n'
vscode = true
dots.pollAll()
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow')
assert(shown:find('kernelcatcher',1,true), shown)
beforeRenders = renders
screenWidths = {5000, 1000}
dots.refresh()
assert(renders == beforeRenders + 1, 'screen changes must recalculate the layout without state changes')
assert(shown:find('T: ●3  |  VS: ●3  |  SSH: ●1',1,true) and shown:match('%d+:%d%d  >$'), shown)
assert(#drawnColors == 3 and drawnColors[1] == '#D946EF'
    and drawnColors[2] == '#D946EF' and drawnColors[3] == '#EAB308',
    'local and remote summaries must use priority colors, and exclude containers from SSH')
local remoteEntries = 0
for _, item in ipairs(dots.menu()) do
    if item.indent == 1 then remoteEntries = remoteEntries + 1 end
    if type(item.title) == 'string' then
        assert(not item.title:find('Expand') and not item.title:find('Collapse')
            and not item.title:find('Automatically'), 'layout controls must not appear in the dropdown')
    end
end
assert(remoteEntries == 4, 'each remote window must appear exactly once in the menu')
screenWidths = {5000}
dots.refresh()
assert(shown:find('kernelcatcher',1,true), 'removing a narrow screen should restore full details')

local longName = string.rep('long-container-name-', 20)
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow', longName)
assert(shown:find('VS: ●3',1,true), 'long labels must collapse even on a wide screen')
poll('done', 'idle', 'bg=red', 'bg=blue', longName)
assert(drawnColors[2] == '#EF4444' and drawnColors[3] == '#3B82F6', 'done must beat idle')
poll('done', 'running', 'bg=red', 'bg=yellow', longName)
assert(drawnColors[2] == '#EF4444' and drawnColors[3] == '#EAB308', 'host running state must not override container done state')
poll('running', 'needs-input', 'bg=yellow', 'bg=#123abc', longName)
assert(drawnColors[2] == '#EAB308' and drawnColors[3] == '#123ABC', 'host waiting state must remain in SSH with its custom color')
poll('idle', nil, 'bg=blue', nil, longName)
assert(shown:find('VS: ●3  |  SSH: ○',1,true) and shown:match('%d+:%d%d  >$'), shown)
poll('idle', nil, 'bg=blue')
assert(shown:find('kernelcatcher',1,true), 'shortening labels without a state change must recalculate width')

-- Automatic compaction must also handle a busy terminal with no remote connections.
vscode, sshOutput = false, ''
dots.pollAll()
localOutput = ''
for i = 1, 80 do
    localOutput = localOutput .. ('@%d\t%d\t1\tagent\t0\tidle\tbg=blue\n'):format(i,i)
end
dots.refresh()
assert(shown:match('^T: ●80  %d+:%d%d  >$') and #drawnColors == 1, shown)
localOutput = '@1\t1\t1\tagent\t1\tidle\tbg=blue\n'
dots.refresh()
assert(shown:match('^T: ◉  %d+:%d%d  <$'), 'small local-only title should expand automatically')
clickFromRight(8)
assert(shown:match('^T: ●1  %d+:%d%d  >$'), 'arrow must work without remotes')
for _, pair in ipairs({{'done', 'red'}, {'running', 'yellow'}, {'needs-input', '#123abc'}}) do
    localOutput = localOutput .. '@2\t2\t1\tagent\t0\t' .. pair[1] .. '\tbg=' .. pair[2] .. '\n'
    dots.refresh()
    assert(drawnColors[1] == dots.styleColor('bg=' .. pair[2]), 'terminal priority mismatch: ' .. pair[1])
end
localOutput = ''
dots.refresh()
assert(shown:match('^T: ○  %d+:%d%d  >$'), 'empty terminal must retain its hollow dot and clock')

vscode, sshOutput = true, '@6\t5\tcolossus\n'
dots.pollAll()
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow')
beforeRenders, beforeSounds = renders, sounds
local beforePopups = popupCount
local _, since = dots.readMode()
clickFromRight(8)
assert(shown:find('kernelcatcher',1,true) and shown:match('%d+:%d%d  <$'), shown)
assert(popupCount == beforePopups, 'arrow clicks must not open the dropdown')
dots.refresh()
assert(renders == beforeRenders + 1, 'manual expansion must survive subsequent refreshes')

-- An expanded title that no longer fits moves into a strip; the top bar keeps its summary and clock.
screenWidths = {1000}
dots.refresh()
local panel = assert(dots.panel(), 'overflow must open the expanded strip')
assert(shown:find('VS: ●3',1,true) and shown:match('%d+:%d%d  <$'), shown)
assert(not shown:find('kernelcatcher',1,true), 'overflow must not expand the menu-bar title')
local function panelText()
    local parts = {}
    for _, element in ipairs(dots.panel().elements) do
        if element.type == 'text' then
            parts[#parts+1] = element.text.text
            assert(element.frame.y + element.frame.h / 2 == dots.panel().bounds.h / 2,
                'dots and labels must share the same vertical center')
        end
        if element.trackMouseUp then
            assert(element.frame.x >= 0 and element.frame.x + element.frame.w <= dots.panel().bounds.w + 0.01,
                'click targets must stay inside the visible strip')
        end
    end
    return table.concat(parts)
end
assert(panelText():find('kernelcatcher',1,true), 'strip must show full container names')
assert(not panelText():find('VS - colossus:',1,true), 'strip must not duplicate the SSH host under VS Code')
assert(panel.bounds.y == 24 and panel.bounds.x >= 0 and panel.bounds.x + panel.bounds.w <= 1000)
assert(panel.bounds.h == 32, 'strip must be a single tight row directly under the menu bar')
assert(panel.bounds.w < 968, 'short contents must not leave a full-width empty strip')
dots.applyPoll('colossus', 0, projects, '')
dots.refresh()
assert(panelText():find('VS - colossus - bionemo-framework',1,true),
    'the floating strip must include the project name')
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow')
local remoteAction
for _, element in ipairs(panel.elements) do
    if element.id and element.id:find('window-',1,true) then remoteAction = element.id; break end
end
assert(remoteAction, 'remote dots need clickable targets')
panel.click(panel, 'mouseUp', remoteAction)
assert(focusedApp == 'com.microsoft.VSCode', 'remote dot must focus its source app')
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow', longName)
assert(panelText():find(longName,1,true), 'long labels must retain their full text for horizontal scrolling')
assert(dots.panel().bounds.h == 32, 'overflow must stay on one row')
local function firstCaptionX()
    for _, element in ipairs(dots.panel().elements) do
        if element.type == 'text' then return element.frame.x end
    end
end
local originalX = firstCaptionX()
dots.panel().click(dots.panel(), 'mouseUp', 'next')
assert(firstCaptionX() < originalX, 'right arrow must reveal overflow horizontally')
panelText()
dots.panel().click(dots.panel(), 'mouseUp', 'previous')
assert(firstCaptionX() == originalX, 'left arrow must return to the first group')
localOutput = '@1\t1\t1\tcodex\t1\tneeds-input\tbg=magenta\n'
dots.refresh()
panel = dots.panel()
for _, element in ipairs(panel.elements) do
    if element.id and element.id:find('window-',1,true) then
        panel.click(panel, 'mouseUp', element.id)
        break
    end
end
assert(selectedWindow and selectedWindow:find('select-window -t @1',1,true), 'local dots must select their window')
assert(focusedApp == 'com.googlecode.iterm2', 'local dot must focus iTerm')
assert(panelText():find('◉',1,true), 'live local state must reach the strip')
screenOrigin = -1000
dots.setExpanded(true, {x = -40, y = 12})
assert(dots.panel().bounds.x >= -1000 and dots.panel().bounds.x + dots.panel().bounds.w <= 0,
    'strip must stay on the screen where expansion was requested')
dots.setMode('dnd')
assert(not dots.panel() and shown:match('%d+:%d%d$'), 'DND must close the strip and retain the clock')
dots.setMode('on')
since = select(2, dots.readMode())
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow', longName)
assert(dots.panel(), 'leaving DND must restore the chosen expanded view')
clickFromRight(8)
assert(not dots.panel() and shown:match('%d+:%d%d  >$'), 'arrow must close an overflowing strip')
screenOrigin, screenWidths = 0, {5000}
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow')
dots.setExpanded(true)
assert(not dots.panel() and shown:find('kernelcatcher',1,true), 'details must return inline when they fit')
localOutput = ''
dots.refresh()
print('PASS: adaptive inline/strip expansion, single-row scrolling, live states, clickable dots, screen bounds, and DND')

clickFromRight(8)
assert(shown:find('VS: ●3',1,true) and shown:match('%d+:%d%d  >$'), shown)
assert(popupCount == beforePopups, 'collapse arrow must not open the dropdown')
screenWidths = {6000}
dots.refresh()
assert(shown:find('VS: ●3',1,true), 'manual collapse must survive a larger screen')
beforeRenders = renders
clickFromRight(32) -- clock, outside the arrow's hit area
assert(popupCount == beforePopups + 1 and renders == beforeRenders, 'clock must open menu without toggling')
assert(popupItems[1].title == 'On' and popupItems[2].title == 'Do Not Disturb', 'mode controls must remain')
clickFromRight(dots.menubar:frame().w - 12) -- leftmost dots
assert(popupCount == beforePopups + 2 and renders == beforeRenders, 'dots must open menu without toggling')
assert(sounds == beforeSounds, 'layout changes must not replay notifications')
local _, afterSince = dots.readMode()
assert(afterSince == since, 'layout changes must not restart the clock')
dots.setMode('dnd')
assert(shown:find('⛔',1,true) and shown:match('%d+:%d%d$'), 'DND must keep the clock and hide the arrow')
clickFromRight(8)
assert(popupCount == beforePopups + 3, 'DND must open the mode menu even at the right edge')
dots.setMode('on')
assert(hotkeyFn, 'toggle hotkey must be bound')
hotkeyFn()
assert(dots.readMode() == 'dnd', 'hotkey must switch On to Do Not Disturb')
hotkeyFn()
assert(dots.readMode() == 'on', 'hotkey must switch Do Not Disturb back to On')
print('PASS: the hotkey toggles Do Not Disturb')
assert(shown:match('%d+:%d%d  >$'), 'DND must preserve the chosen compact layout')
print('PASS: automatic sizing, local/remote summary priority, direct arrow clicks, clock, and menu access')

assert(dots.expansionHotkey and expansionFn, 'F9 must bind expansion by default')
local modeBefore, sinceBefore = dots.readMode()
expansionFn()
assert(shown:match('%d+:%d%d  <$'), 'F9 must expand inline when the details fit')
expansionFn()
assert(shown:match('%d+:%d%d  >$'), 'F9 must collapse inline details')
screenWidths = {1000}
poll('idle', 'idle', 'bg=blue', 'bg=blue', longName)
expansionFn()
assert(dots.panel(), 'F9 must open the overflow strip')
expansionFn()
assert(not dots.panel(), 'F9 must close the overflow strip')
assert(select(1, dots.readMode()) == modeBefore and select(2, dots.readMode()) == sinceBefore,
    'expansion must not change mode or restart the clock')
dots.setMode('dnd')
expansionFn()
assert(not dots.panel() and dots.readMode() == 'dnd', 'F9 must respect Do Not Disturb')
dots.setMode('on')
screenWidths = {6000}
print('PASS: F9 toggles inline and floating expansion without changing notification mode')

sshOutput = '@6\t5\tcolossus\n@7\t6\tcolossus\n'
dots.pollAll()
assert(#dots.sshLinks() == 2 and #dots.segments() == 2, 'color every SSH window but keep one segment per host')
local hostPoll = 'T\thost\tcolossus\nW\tcontainer:1000\tcontainer\t@0\t0\tagent\t0\tmain\tneeds-input\tbg=magenta\n'
dots.applyPoll('colossus', 0, hostPoll, '')
dots.refresh()
assert(sshOptions['@6']['@ssh_agent_color'] == 'green', 'containers must not color the SSH host')
for i, pair in ipairs({{'idle', 'blue'}, {'done', 'red'}, {'running', 'yellow'}, {'needs-input', '#123abc'}}) do
    -- Lower-priority windows follow the winner to catch last-event-wins behavior.
    hostPoll = hostPoll .. ('W\thost\tcolossus\t@%d\t%d\tagent\t0\tmain\t%s\tbg=%s\n'):format(i, i, pair[1], pair[2])
    dots.applyPoll('colossus', 0, hostPoll .. 'W\thost\tcolossus\t@20\t20\tagent\t0\tmain\tidle\tbg=blue\n', '')
    dots.refresh()
    for _, id in ipairs({'@6', '@7'}) do
        assert(sshOptions[id]['@ssh_agent_color'] == pair[2], 'wrong SSH summary color: ' .. pair[1])
    end
end
local writesBefore = sshWrites
dots.refresh()
assert(sshWrites == writesBefore, 'unchanged polls must not write tmux options')
dots.applyPoll('colossus', 255, '', 'unreachable')
dots.refresh()
assert(sshOptions['@6']['@ssh_agent_color'] == '', 'failed polls must clear stale SSH colors')
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow')
sshOutput = '@6\t5\tother-host\n@7\t6\t\n'
dots.refresh()
assert(sshOptions['@6']['@ssh_agent_host'] == 'other-host' and sshOptions['@6']['@ssh_agent_color'] == '',
    'reconnecting to another host must not retain its predecessor color')
assert(sshOptions['@7']['@ssh_agent_host'] == '' and sshOptions['@7']['@ssh_agent_color'] == '',
    'disconnect must clear published colors')
sshOutput = '@6\t5\tcolossus\n'
poll('needs-input', 'needs-input', 'bg=magenta', 'bg=magenta')
local colorCommand = assert(io.open(arg[2] .. '/ssh-color-command.txt', 'w'))
colorCommand:write(sshCommand)
colorCommand:close()
print('PASS: SSH titles share host priority, update duplicate windows, and clear disconnected/failed polls')

startTasks = true
dots.pollAll()
local first = tasks[#tasks]
assert(dots.remote.colossus.task == first)
first.running = false -- Process exited; Hammerspoon has not delivered its callback yet.
dots.pollAll()
assert(tasks[#tasks] == first, 'wait for a pending callback before starting another poll')
first.callback(0, hostPoll, '')
assert(dots.remote.colossus.ok and dots.remote.colossus.task == nil)
dots.pollAll()
local expired = tasks[#tasks]
dots.remote.colossus.started = os.time() - 21
dots.pollAll()
local replacement = tasks[#tasks]
assert(expired.terminated and replacement ~= expired, 'expire a stalled probe and retry')
expired.callback(0, hostPoll, '')
assert(dots.remote.colossus.task == replacement and not dots.remote.colossus.ok,
    'a late success must not replace the timeout or clear the new task')
replacement.callback(0, hostPoll, '')
assert(dots.remote.colossus.ok and dots.remote.colossus.task == nil)
dots.pollAll()
local removed = tasks[#tasks]
vscode, sshOutput = false, ''
dots.pollAll()
assert(removed.terminated and not dots.remote.colossus, 'removed hosts must stop polling')
sshOutput = '@6\t5\tcolossus\n'
dots.pollAll()
local reconnected = tasks[#tasks]
removed.callback(255, '', 'old failure')
assert(dots.remote.colossus.task == reconnected and dots.remote.colossus.err == nil,
    'a removed host callback must not corrupt a reconnected host')
reconnected.callback(0, hostPoll, '')
vscode, startTasks = true, false
print('PASS: foreground probes, pending completions, timeouts, late callbacks, and host reconnection')

-- Completion events are independent of transient colors and deduplicate linked sessions.
vscode = false
dots.pollAll()
local function completionPoll(state, event, duplicate)
    local row = 'W\thost\tcolossus\t@70\t70\tagents\t0\tmain\t' .. state
        .. '\tbg=red\tSSH\t' .. (event:match('^[^:]+:%d+$') and event .. ':1' or event) .. '\n'
    dots.applyPoll('colossus', 0, 'T\thost\tcolossus\n' .. row .. (duplicate and row or ''), '')
end
beforeSounds = sounds
completionPoll('running', 'server-a:0')
completionPoll('done', 'server-a:0')
assert(sounds == beforeSounds, 'a temporary done color must not bypass pending review')
completionPoll('running', 'server-a:0')
completionPoll('done', 'server-a:1', true)
assert(sounds == beforeSounds + 1, 'one completion event must produce one sound across linked sessions')
completionPoll('idle', 'server-a:1')
completionPoll('done', 'server-a:1')
assert(sounds == beforeSounds + 1, 'focus and repeated polls must not replay a completion')
completionPoll('done', 'server-b:1')
assert(sounds == beforeSounds + 1, 'a restored server must establish a new baseline silently')
completionPoll('done', 'server-b:2')
assert(sounds == beforeSounds + 2, 'new work on the restored server must notify')
completionPoll('done', 'server-b:3:0')
assert(sounds == beforeSounds + 2, 'new work must suppress an old completion even during a red handoff gap')
completionPoll('done', 'server-b:3:1')
assert(sounds == beforeSounds + 2, 'readiness alone must not replay a suppressed event')
completionPoll('done', 'server-b:4:1')
assert(sounds == beforeSounds + 3, 'the later completion must still notify')
print('PASS: remote completion decisions, delayed handoffs, linked-window deduplication, and server restarts')

-- The Python harness runs these exact queries against an isolated tmux server.
local arguments = assert(io.open(arg[2] .. '/ssh-args.txt', 'w'))
for _, value in ipairs(first.args) do arguments:write(value, '\n') end
arguments:close()
local probe = assert(io.open(arg[2] .. '/probe.sh', 'w'))
probe:write(remoteScript)
probe:close()
local query = assert(io.open(arg[2] .. '/query.txt', 'w'))
query:write(localCommand)
query:close()
local fixturesFile = io.open(arg[2] .. '/styles.tsv', 'r')
if fixturesFile then
    for line in fixturesFile:lines() do
        local expected, style = line:match('^([^\t]+)\t(.*)$')
        assert(dots.styleColor(style) == expected, 'tmux style mismatch: ' .. line)
    end
    fixturesFile:close()
    print('PASS: real tmux current, inactive, inherited, and last-window styles match dot colors')
end

-- A key macOS already uses must not be bound; the menu says so and still offers the mode switch.
hotkeyFn, activeHotkeys = nil, {}
AGENT_DOTS = { hotkey = { mods = {}, key = 'F11' }, expansionHotkey = false }
local busy = dofile(arg[1])
assert(hotkeyFn == nil and busy.hotkey == nil, 'system shortcut must not be bound')
local busyMenu = busy.menu()
assert(busyMenu[1].title == 'On' and busyMenu[2].title == 'Do Not Disturb', 'mode controls must remain')
assert(busyMenu[3].disabled and busyMenu[3].title:find('F11', 1, true), 'menu must report the unavailable key')
print('PASS: a busy hotkey is reported in the menu instead of being bound')
assert(busy.expansionHotkey == nil, 'expansionHotkey=false must disable expansion binding')
activeHotkeys = {{idx = ':F9'}}
AGENT_DOTS = {hotkey = false}
busy = dofile(arg[1])
assert(busy.expansionHotkey == nil and #activeHotkeys == 1, 'F9 must not shadow an existing Hammerspoon shortcut')
assert(busy.menu()[3].title:find('F9', 1, true), 'menu must report an occupied expansion key')
activeHotkeys, hotkeyFn = {}, nil
AGENT_DOTS = {hotkey = false, expansionHotkey = {mods = {'ctrl'}, key = 'F8'}}
local custom = dofile(arg[1])
assert(custom.expansionHotkey.idx == 'ctrl:F8' and hotkeyFn, 'init.lua must be able to override the expansion key')
print('PASS: expansion shortcut respects existing bindings, overrides, and disabling')

local originalDofile, badgeModes = dofile, {}
dofile = function(path)
    if path:match('/slack_badge%.lua$') then
        return {new = function() return {
            sync = function(mode) badgeModes[#badgeModes + 1] = mode end,
            status = function() return 'Slack badge: fixture failure' end,
        } end}
    end
    return originalDofile(path)
end
AGENT_DOTS = {hotkey = false, expansionHotkey = false, slackBadge = true}
local integrated = dofile(arg[1])
integrated.setMode('dnd')
assert(badgeModes[#badgeModes] == 'dnd', 'DND must reach the badge integration')
local modeFile = assert(io.open(arg[2] .. '/.local/state/agent-dots/mode', 'w'))
modeFile:write('on 123\n'):close()
integrated.refresh()
assert(badgeModes[#badgeModes] == 'on', 'external mode-file changes must reach the badge integration')
assert(integrated.menu()[3].title == 'Slack badge: fixture failure', 'badge failure must be visible in the menu')
assert(integrated.menu()[1].fn and integrated.menu()[2].fn, 'badge failure must leave mode controls usable')
dofile = originalDofile
print('PASS: optional Slack badge synchronization follows menu and external mode changes')
