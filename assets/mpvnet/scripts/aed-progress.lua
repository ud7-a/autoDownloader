-- Auto Episodes Downloader: note what was played and how far, so the app's
-- Library can tick episodes off. JSON lines are appended to aed-progress.log in
-- this config folder. Nothing else is read or sent anywhere.
--
-- A line is written when a file stops playing (it ended, the next one started,
-- or the player closed), and also every 30 seconds of playback and on pause --
-- so a crash or a force-close loses at most the last half minute, not the
-- whole episode. The app reads every line and keeps the furthest point.
local utils = require "mp.utils"

local CHECKPOINT_SECONDS = 30

local log_path = mp.command_native({"expand-path", "~~/aed-progress.log"})
local current, percent, written, last_logged = nil, 0, true, -1

local function append(line)
    -- mpv's own writer handles non-ASCII paths; io.open is the fallback for
    -- builds that predate utils.append_file.
    local ok, res = pcall(utils.append_file, "file://" .. log_path, line)
    if ok and res then return end
    local f = io.open(log_path, "a")
    if f then
        f:write(line)
        f:close()
    end
end

local function log_now(eof)
    append(utils.format_json({path = current, percent = percent, eof = eof}) .. "\n")
    last_logged = percent
end

-- The file stopped playing: its final line, once.
local function report(eof)
    if not current or written then return end
    written = true
    log_now(eof)
end

-- Part-way: only when playback actually moved since the last line.
local function checkpoint()
    if not current or written then return end
    if math.abs(percent - last_logged) < 1 then return end
    log_now(false)
end

mp.observe_property("percent-pos", "number", function(_, value)
    if value then percent = value end
end)

mp.register_event("file-loaded", function()
    local path = mp.get_property("path")
    if path and not path:match("^%a[%w+.-]*://") then
        current = utils.join_path(mp.get_property("working-directory") or "", path)
    else
        current = nil   -- a stream, not an episode on disk
    end
    percent, written, last_logged = 0, false, -1
end)

mp.observe_property("pause", "bool", function(_, paused)
    if paused then checkpoint() end
end)
mp.add_periodic_timer(CHECKPOINT_SECONDS, checkpoint)

mp.register_event("end-file", function(event) report(event.reason == "eof") end)
mp.register_event("shutdown", function() report(false) end)
