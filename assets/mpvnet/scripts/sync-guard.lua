-- Smooth frame timing (video-sync=display-resample + tscale=oversample) needs
-- the GPU to finish each frame with room to spare. When it can't -- e.g. the
-- anime profile's largest Anime4K models (F1) on a mid-range GPU -- frames are
-- dropped and the episode stutters, while plain audio sync would play smoothly.
-- This watches for that and falls back to audio sync for that shader set, then
-- goes back to smooth timing when the profile (shader set) changes to a lighter one.
--
-- Measured on an RTX 3050 at 300 Hz: the former Large anime chain drops/delays
-- ~0 per 20 s; the UL chain ~500 per 20 s. The threshold sits far from both.
local CHECK_SECONDS = 1
local WINDOW = 4          -- seconds of history looked at
local LIMIT = 8           -- dropped + late frames in WINDOW that mean "too slow"
local GRACE = 3           -- seconds ignored after load, seek or a sync change

local too_heavy = {}      -- glsl-shaders string -> true, for this session
local history, last, quiet_until = {}, nil, 0
local fell_back = false   -- audio sync was set by this script, not by the user

local function now() return mp.get_time() end
local function shaders() return mp.get_property("glsl-shaders") or "" end

local function counters()
    return (mp.get_property_number("frame-drop-count", 0) or 0)
        + (mp.get_property_number("vo-delayed-frame-count", 0) or 0)
end

local function settle()
    history, last, quiet_until = {}, nil, now() + GRACE
end

local function fall_back()
    too_heavy[shaders()] = true
    fell_back = true
    mp.set_property("video-sync", "audio")
    mp.msg.info("display sync dropped frames with this shader set: audio sync")
    mp.osd_message("Smooth frame timing off: the GPU can't keep up with this profile\n"
        .. "(audio sync instead, no stutter -- F12 to try again)", 5)
end

local function check()
    local sync = mp.get_property("video-sync") or ""
    if sync:sub(1, 8) ~= "display-" or mp.get_property_bool("pause")
            or mp.get_property_bool("seeking") or now() < quiet_until then
        last = nil
        return
    end
    local c = counters()
    if last then
        history[#history + 1] = c - last
        if #history > WINDOW then table.remove(history, 1) end
        local sum = 0
        for _, n in ipairs(history) do sum = sum + n end
        if sum >= LIMIT then fall_back() end
    end
    last = c
end

mp.add_periodic_timer(CHECK_SECONDS, check)
mp.register_event("file-loaded", settle)
mp.register_event("playback-restart", settle)

-- F12 (or anything else) changed the sync mode: start measuring afresh. A
-- switch back to display sync by the user is theirs -- forget the fallback.
mp.observe_property("video-sync", "string", function(_, value)
    settle()
    if value and value:sub(1, 8) == "display-" then fell_back = false end
end)

-- Profile switch (F1/F2): a set already known to be too heavy goes straight
-- to audio sync; leaving it restores smooth timing if this script removed it.
mp.observe_property("glsl-shaders", "native", function()
    settle()
    if too_heavy[shaders()] then
        if (mp.get_property("video-sync") or ""):sub(1, 8) == "display-" then fall_back() end
    elseif fell_back then
        fell_back = false
        mp.set_property("video-sync", "display-resample")
    end
end)
