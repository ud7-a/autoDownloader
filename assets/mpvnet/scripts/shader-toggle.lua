-- Toggle all glsl-shaders off/on for before/after comparison.
-- Restores whatever the active profile had loaded.
-- Bound unqualified in input.conf: newer mpv renames this script to
-- "shader_toggle", so "shader-toggle/toggle" silently stops matching.
local saved = nil

mp.add_key_binding(nil, "shader-compare-toggle", function()
    local current = mp.get_property_native("glsl-shaders")
    if #current > 0 then
        saved = current
        mp.set_property_native("glsl-shaders", {})
        mp.osd_message("Shaders: OFF (compare)")
    elseif saved then
        mp.set_property_native("glsl-shaders", saved)
        saved = nil
        mp.osd_message("Shaders: ON")
    else
        mp.osd_message("No shaders to restore (press F1/F2)")
    end
end)
