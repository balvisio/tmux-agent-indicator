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
hs = {
    execute = function(command)
        if command:find('/bin/ps',1,true) then
            return vscode and '10 1 Code remote-ssh\n11 10 /usr/bin/ssh colossus\n' or '', true
        elseif command:find('@ssh_host',1,true) then
            return '@6\t5\tcolossus\n', true
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
        if (text == '●' or text == '◉') and attrs.color then
            menuColors[#menuColors+1] = attrs.color.hex
        end
        return text
    end},
    menubar = {new = function() return {
        setMenu = function() end,
        isInMenuBar = function() return true end,
        setTitle = function(_,text) shown=text; renders=renders+1; drawnColors=menuColors; menuColors={} end,
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
local function poll(containerState,hostState,style)
    style = style or 'bg=magenta,fg=black'
    local text = 'T\thost\tcolossus\nT\tcontainer:1000\tkernelcatcher\n'
    for i=0,2 do
        text=text..('W\tcontainer:1000\tkernelcatcher\t@%d\t%d\tagent\t0\tmain\t%s\t%s\n'):format(i,i+1,containerState,style)
    end
    if hostState then text=text..'W\thost\tcolossus\t@0\t1\thost-agent\t0\tmain\t'..hostState..'\t'..style..'\n' end
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
