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
local sshOutput = '@6\t5\tcolossus\n'
local sshOptions = {}
local sshWrites = 0
local sshCommand
local mousePoint = {x = 0, y = 0}
local popupCount = 0
local popupItems = nil
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
    return setmetatable({text = a.text .. b.text, colors = colors}, styled)
end
styled.__index = {find = function(self, ...) return self.text:find(...) end}
hs = {
    execute = function(command)
        if command:find('set-option -wq',1,true) then
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
        return setmetatable({text = text, colors = colors}, styled)
    end},
    drawing = {getTextDrawingSize = function(text) return {w = utf8.len(text.text) * 4} end},
    screen = {allScreens = function()
        local screens = {}
        for _, width in ipairs(screenWidths) do
            screens[#screens+1] = {fullFrame = function() return {w = width} end}
        end
        return screens
    end},
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
    task = {new = function() return {start=function() return false end,isRunning=function() return false end} end},
    host = {interfaceStyle=function() return 'Dark' end},
}
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
assert(shown:find('T: ●3  |  VS: ●4  |  SSH: ●1',1,true) and shown:match('%d+:%d%d  >$'), shown)
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
assert(remoteEntries == 5, 'compact layout must preserve every remote window in the menu')
screenWidths = {5000}
dots.refresh()
assert(shown:find('kernelcatcher',1,true), 'removing a narrow screen should restore full details')

local longName = string.rep('long-container-name-', 20)
poll('needs-input', 'running', 'bg=magenta', 'bg=yellow', longName)
assert(shown:find('VS: ●4',1,true), 'long labels must collapse even on a wide screen')
poll('done', 'idle', 'bg=red', 'bg=blue', longName)
assert(drawnColors[2] == '#EF4444' and drawnColors[3] == '#3B82F6', 'done must beat idle')
poll('done', 'running', 'bg=red', 'bg=yellow', longName)
assert(drawnColors[2] == '#EAB308', 'running must beat done')
poll('running', 'needs-input', 'bg=yellow', 'bg=#123abc', longName)
assert(drawnColors[2] == '#123ABC', 'waiting must beat running and retain its actual custom color')
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
clickFromRight(8)
assert(shown:find('VS: ●4',1,true) and shown:match('%d+:%d%d  >$'), shown)
assert(popupCount == beforePopups, 'collapse arrow must not open the dropdown')
screenWidths = {6000}
dots.refresh()
assert(shown:find('VS: ●4',1,true), 'manual collapse must survive a larger screen')
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
assert(shown:match('%d+:%d%d  >$'), 'DND must preserve the chosen compact layout')
print('PASS: automatic sizing, local/remote summary priority, direct arrow clicks, clock, and menu access')

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

-- The Python harness runs these exact queries against an isolated tmux server.
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
