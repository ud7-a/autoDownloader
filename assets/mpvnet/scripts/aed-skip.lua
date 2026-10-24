-- Auto Episodes Downloader: skip an episode's opening / ending.
--
-- The app finds them from the audio (utils/intro_detect.py) and keeps the times
-- in a hidden .aed-skip.json beside the episodes; this reads the entry for the
-- file that's playing. What happens is set on the app's Video Player tab and
-- saved to script-opts/aed-skip.conf:
--   off   nothing
--   ask   "Skip opening  K" on screen for its first 10 seconds; K skips until
--         the song ends (default for openings -- a wrong guess is just a
--         prompt to ignore)
--   auto  skipped straight away; K within a few seconds goes back
-- Skipping goes to the end of the song, not of the file, so a scene after the
-- credits still plays. Seeking back into a segment never skips it again.
-- K is taken (forced) only while one of these shows; input.conf binds it to
-- "ignore" otherwise, or mpv.net's own K (open input.conf) would answer.
local utils = require "mp.utils"
local options = require "mp.options"

local o = {opening = "ask", ending = "off", prompt_seconds = 10}
options.read_options(o, "aed-skip")

local NAMES = {op = "opening", ed = "ending"}
local KEY = "k"

local segments = {}     -- {kind, start, stop}
local handled = {}      -- kind -> true once skipped (or gone back) in this file
local prompt = nil      -- segment K skips right now
local text_until = nil  -- its "Skip ..." text hides at this time-pos
local undo = nil        -- {seg, until} after an automatic skip
local overlay = mp.create_osd_overlay("ass-events")

local function mode(kind) return kind == "op" and o.opening or o.ending end

local function read_json(path)
    local f = io.open(path, "rb")       -- mpv's io handles non-ASCII paths
    if not f then return nil end
    local text = f:read("*a")
    f:close()
    return utils.parse_json(text)
end

local function show(text)
    overlay.data = "{\\an3\\fs30\\bord2\\shad0\\1c&HFFFFFF&\\3c&H202020&}" .. text
    overlay:update()
end

local function hide()
    overlay:remove()
    mp.remove_key_binding("aed-skip")
end

local function clear()
    prompt, undo, text_until = nil, nil, nil
    hide()
end

local function seek(t)
    mp.commandv("seek", string.format("%.3f", t), "absolute+exact")
end

local function skip(seg)
    handled[seg.kind] = true
    clear()
    seek(seg.stop)
end

local function load()
    segments, handled = {}, {}
    clear()
    local path = mp.get_property("path")
    if not path or path:match("^%a[%w+.-]*://") then return end
    path = utils.join_path(mp.get_property("working-directory") or "", path)
    local dir, name = utils.split_path(path)
    local data = read_json(utils.join_path(dir, ".aed-skip.json"))
    local entry = data and data.episodes and data.episodes[name]
    if type(entry) ~= "table" then return end
    local info = utils.file_info(path)
    if not info or info.size ~= entry.size then return end   -- file replaced: stale
    for _, kind in ipairs({"op", "ed"}) do
        local s = entry[kind]
        if type(s) == "table" and tonumber(s[1]) and tonumber(s[2]) and mode(kind) ~= "off" then
            segments[#segments + 1] = {kind = kind, start = s[1], stop = s[2]}
        end
    end
end

local function tick(_, t)
    if not t or #segments == 0 then return end
    if undo and t > undo["until"] then clear() end
    for _, seg in ipairs(segments) do
        local inside = t >= seg.start - 0.3 and t < seg.stop - 1
        if inside and not handled[seg.kind] then
            if mode(seg.kind) == "auto" then
                skip(seg)
                undo = {seg = seg, ["until"] = seg.stop + 5}
                show("Skipped the " .. NAMES[seg.kind] .. "  ·  K to go back")
                mp.add_forced_key_binding(KEY, "aed-skip", function()
                    local back = undo and undo.seg
                    clear()
                    if back then seek(back.start) end
                end)
            else
                -- The text is shown briefly; K keeps working until the song ends.
                if prompt ~= seg then
                    prompt = seg
                    text_until = t + o.prompt_seconds
                    show("Skip " .. NAMES[seg.kind] .. "  ▸  K")
                    mp.add_forced_key_binding(KEY, "aed-skip", function()
                        if prompt then skip(prompt) end
                    end)
                elseif text_until and t >= text_until then
                    text_until = nil
                    overlay:remove()
                end
                return
            end
        end
    end
    if prompt then clear() end          -- the song ended, or a seek left it
end

mp.register_event("file-loaded", load)
mp.register_event("end-file", function() segments = {}; clear() end)
mp.observe_property("time-pos", "number", tick)
