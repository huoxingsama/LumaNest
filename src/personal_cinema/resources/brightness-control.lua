-- Live renderer brightness: preview while dragging, persist once on release.
-- Uses mpv's color adjustment, including HDR/Dolby Vision; no video conversion.
local mp = require 'mp'
local assdraw = require 'mp.assdraw'
local input = require 'mp.input'
local overlay = mp.create_osd_overlay('ass-events')
overlay.z = 1200
local visible, dragging, dirty = false, false, false
local value, original = 0, 0
local layout, preview_timer, save_timer
local bindings = {}
local draw, close, open_number

local function clamp(v) return math.max(-100, math.min(100, math.floor(v + 0.5))) end
local function signed(v) return v > 0 and ('+' .. v) or tostring(v) end
local function inside(x, y, r)
    return r and x >= r.x and x <= r.x + r.w and y >= r.y and y <= r.y + r.h
end
local function mouse()
    local p = mp.get_property_native('mouse-pos') or {}
    return p.x or -1, p.y or -1
end
local function apply_preview()
    preview_timer = nil
    if not visible then return end
    local ok, err = mp.set_property_number('brightness', value)
    if not ok then mp.osd_message('亮度未应用：' .. tostring(err), 3) end
end
local function set_value(v)
    v = clamp(v)
    if v == value then return end
    value = v
    dirty = value ~= original
    if not preview_timer then preview_timer = mp.add_timeout(1/30, apply_preview) end
    draw()
end
local function save()
    if save_timer then save_timer:kill(); save_timer = nil end
    if preview_timer then preview_timer:kill(); preview_timer = nil end
    if not dirty then return end
    local ok, err = mp.set_property_number('brightness', value)
    if not ok then mp.osd_message('亮度未应用：' .. tostring(err), 3); return end
    -- The shared owner is the only writer of quality-state.json.
    mp.commandv('script-message', 'shared-quality', 'set', 'brightness', tostring(value))
    original, dirty = value, false
end
local function rounded(a, r, radius, color, alpha)
    local x,y,w,h = r.x,r.y,r.w,r.h
    radius = math.min(radius, w/2, h/2)
    local k = radius * 0.55228475
    a:new_event()
    a:append(string.format('{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H%s&\\alpha&H%s&\\p1}', color, alpha or '00'))
    a:append(string.format('m %g %g l %g %g b %g %g %g %g %g %g l %g %g b %g %g %g %g %g %g l %g %g b %g %g %g %g %g %g l %g %g b %g %g %g %g %g %g',
        x+radius,y,x+w-radius,y,x+w-radius+k,y,x+w,y+radius-k,x+w,y+radius,
        x+w,y+h-radius,x+w,y+h-radius+k,x+w-radius+k,y+h,x+w-radius,y+h,
        x+radius,y+h,x+radius-k,y+h,x,y+h-radius+k,x,y+h-radius,
        x,y+radius,x,y+radius-k,x+radius-k,y,x+radius,y))
    a:append('{\\p0}')
end
local function text(a,x,y,size,label,color,align)
    a:new_event()
    a:append(string.format('{\\an%d\\pos(%g,%g)\\fnMicrosoft YaHei UI\\fs%g\\bord0\\shad0\\1c&H%s&\\alpha&H00&}%s',
        align or 7,x,y,size,color or 'F4F4F4',label))
end
draw = function()
    if not visible then return end
    local w,h = mp.get_osd_size()
    if not w or w < 320 or not h or h < 240 then overlay:remove(); return end
    local s = math.max(0.6, math.min(w/1600, h/900, 1.5))
    local pw,ph = math.min(w-32,620*s),252*s
    local x,y = (w-pw)/2,(h-ph)/2
    local track={x=x+36*s,y=y+130*s,w=pw-72*s,h=8*s}
    layout={panel={x=x,y=y,w=pw,h=ph},track=track,
        number={x=x+pw-132*s,y=y+16*s,w=108*s,h=46*s},
        hit={x=track.x-10*s,y=track.y-19*s,w=track.w+20*s,h=46*s},
        reset={x=x+28*s,y=y+194*s,w=164*s,h=38*s},
        done={x=x+pw-116*s,y=y+194*s,w=88*s,h=38*s}}
    local a=assdraw.ass_new()
    rounded(a,layout.panel,22*s,'272321','0C')
    text(a,x+30*s,y+23*s,25*s,'画面亮度')
    text(a,x+pw-30*s,y+22*s,30*s,signed(value),'FFFFFF',9)
    local trc=mp.get_property('video-params/gamma','')
    local hdr=trc=='pq' or trc=='hlg' or trc=='st2084'
    text(a,x+30*s,y+64*s,16*s,hdr and 'HDR · 拖动预览，点击数值可直接输入' or '拖动预览，点击数值可直接输入','C3BFBC')
    rounded(a,track,4*s,'625C58')
    local pos=track.x+(value+100)/200*track.w
    local zero=track.x+track.w/2
    rounded(a,{x=math.min(zero,pos),y=track.y,w=math.max(1,math.abs(pos-zero)),h=track.h},3*s,'E9DDD2')
    rounded(a,{x=pos-10*s,y=track.y-6*s,w=20*s,h=20*s},10*s,'FFFFFF')
    text(a,track.x,y+151*s,14*s,'-100','AFA9A5')
    text(a,zero,y+151*s,14*s,'0 原始','AFA9A5',8)
    text(a,track.x+track.w,y+151*s,14*s,'+100','AFA9A5',9)
    rounded(a,layout.reset,19*s,'443F3B')
    rounded(a,layout.done,19*s,'EBE8E5')
    text(a,layout.reset.x+layout.reset.w/2,layout.reset.y+layout.reset.h/2,16*s,'恢复原始亮度','F4F4F4',5)
    text(a,layout.done.x+layout.done.w/2,layout.done.y+layout.done.h/2,16*s,'完成','272321',5)
    overlay.res_x,overlay.res_y,overlay.data=w,h,a.text
    overlay:update()
end
local function drag_at(x)
    if layout then set_value((x-layout.track.x)/layout.track.w*200-100) end
end
local function click(event)
    if not visible or not layout then return end
    local x,y=mouse()
    if event.event=='up' then
        if dragging then drag_at(x); dragging=false; save(); draw() end
        return
    end
    if event.event~='down' and event.event~='press' then return end
    if inside(x,y,layout.hit) then
        dragging=true; drag_at(x)
        if event.event=='press' then dragging=false; save() end
    elseif inside(x,y,layout.number) then open_number()
    elseif inside(x,y,layout.reset) then set_value(0); save(); draw()
    elseif inside(x,y,layout.done) or not inside(x,y,layout.panel) then close() end
end
local function step(amount)
    set_value(value+amount)
    if save_timer then save_timer:kill() end
    save_timer=mp.add_timeout(0.25,save)
end
local function bind(key,name,fn,flags)
    name='brightness-control-'..name
    bindings[#bindings+1]=name
    mp.add_forced_key_binding(key,name,fn,flags)
end
close = function()
    save(); visible=false; dragging=false; overlay:remove()
    for _,name in ipairs(bindings) do mp.remove_key_binding(name) end
    bindings={}
end
local function open()
    if mp.get_property_bool('idle-active',true) or not mp.get_property_native('video-params') then
        mp.osd_message('请先打开视频，再调整画面亮度',3); return
    end
    if visible then draw(); return end
    value=clamp(mp.get_property_number('brightness',0)); original=value
    visible,dirty,dragging=true,false,false
    bind('MBTN_LEFT','drag',click,{complex=true})
    bind('MBTN_LEFT_DBL','double',function() end)
    bind('ESC','escape',close)
    bind('ENTER','done',close)
    bind('LEFT','lower',function() step(-1) end,{repeatable=true})
    bind('RIGHT','higher',function() step(1) end,{repeatable=true})
    bind('WHEEL_UP','wheel-up',function() step(1) end,{repeatable=true})
    bind('WHEEL_DOWN','wheel-down',function() step(-1) end,{repeatable=true})
    draw()
end
open_number = function()
        if visible then close() end
        input.get({prompt='画面亮度（-100 到 100，0 为原始亮度）：',
            default_text=tostring(mp.get_property_number('brightness',0)),
            submit=function(raw)
                input.terminate()
                local number=tonumber(raw)
                if not number or number~=number or number < -100 or number > 100 then
                    mp.osd_message('请输入 -100 到 100 之间的数值',3); return
                end
                mp.commandv('script-message','shared-quality','set','brightness',tostring(clamp(number)))
                mp.add_timeout(0.15,function()
                    mp.osd_message('画面亮度：'..signed(clamp(mp.get_property_number('brightness',0))),3)
                end)
            end})
end
mp.register_script_message('brightness-control',function(action)
    if action=='open' then open() elseif action=='close' then close()
    elseif action=='number' then open_number() end
end)
mp.observe_property('mouse-pos','native',function(_,p)
    if visible and dragging and p then drag_at(p.x or 0) end
end)
mp.observe_property('osd-dimensions','native',function() if visible then draw() end end)
mp.observe_property('video-params','native',function() if visible then draw() end end)
mp.observe_property('brightness','number',function(_,actual)
    if visible and not dragging and not dirty and actual then
        value=clamp(actual); original=value; draw()
    end
end)
mp.register_event('end-file',function() if visible then close() end end)

-- select.lua parses menu.conf once. Inject the new entry into its live menu
-- for the current session too, without reloading either the video or scripts.
local function ensure_menu()
    local menu=mp.get_property_native('menu-data')
    if type(menu)~='table' or #menu==0 then return end
    local command='script-message brightness-control open'
    local disabled=mp.get_property_bool('idle-active',true)
    for _,item in ipairs(menu) do
        if item.cmd==command then
            local old=item.state or {}
            if (#old>0 and old[1]=='disabled')==disabled then return end
            item.state=disabled and {'disabled'} or {}
            mp.set_property_native('menu-data',menu)
            return
        end
    end
    table.insert(menu,math.min(3,#menu+1),{title='画面亮度…',cmd=command,
        state=disabled and {'disabled'} or {}})
    mp.set_property_native('menu-data',menu)
end
mp.observe_property('menu-data','native',ensure_menu)
mp.observe_property('idle-active','bool',ensure_menu)
