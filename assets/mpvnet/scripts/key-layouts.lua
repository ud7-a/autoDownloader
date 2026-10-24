-- Auto Episodes Downloader: keyboard shortcuts on non-Latin keyboard layouts.
--
-- mpv names a letter key by the character the active layout types, so with an
-- Arabic layout the K key arrives as "ن" and nothing bound to "k" fires -- not
-- input.conf, not mpv.net's defaults, not scripts such as aed-skip. (Ctrl+S
-- arrives as "Ctrl+س".) mpv has no layout-independent key option, so this binds
-- each such character to the key it sits on and presses that key instead.
--
-- Weak bindings: a binding for the same character in input.conf still wins.
-- A character that is also a Latin key (Shift+D types "]" on the Arabic layout)
-- can't be told apart from that key and is left alone, as are keys that type two
-- characters (Arabic B types "لا").
local LAYOUTS = {
    -- Arabic (101)
    {["ذ"] = "`", ["ض"] = "q", ["ص"] = "w", ["ث"] = "e", ["ق"] = "r", ["ف"] = "t",
     ["غ"] = "y", ["ع"] = "u", ["ه"] = "i", ["خ"] = "o", ["ح"] = "p", ["ج"] = "[",
     ["د"] = "]", ["ش"] = "a", ["س"] = "s", ["ي"] = "d", ["ب"] = "f", ["ل"] = "g",
     ["ا"] = "h", ["ت"] = "j", ["ن"] = "k", ["م"] = "l", ["ك"] = ";", ["ط"] = "'",
     ["ئ"] = "z", ["ء"] = "x", ["ؤ"] = "c", ["ر"] = "v", ["ى"] = "n", ["ة"] = "m",
     ["و"] = ",", ["ز"] = ".", ["ظ"] = "/",
     -- with Shift
     ["ّ"] = "~", ["َ"] = "Q", ["ً"] = "W", ["ُ"] = "E", ["ٌ"] = "R", ["إ"] = "Y",
     ["‘"] = "U", ["÷"] = "I", ["×"] = "O", ["؛"] = "P", ["ِ"] = "A", ["ٍ"] = "S",
     ["أ"] = "H", ["ـ"] = "J", ["،"] = "K", ["ْ"] = "X", ["آ"] = "N", ["’"] = "M",
     ["؟"] = "?"},
    -- Russian (ЙЦУКЕН)
    {["ё"] = "`", ["й"] = "q", ["ц"] = "w", ["у"] = "e", ["к"] = "r", ["е"] = "t",
     ["н"] = "y", ["г"] = "u", ["ш"] = "i", ["щ"] = "o", ["з"] = "p", ["х"] = "[",
     ["ъ"] = "]", ["ф"] = "a", ["ы"] = "s", ["в"] = "d", ["а"] = "f", ["п"] = "g",
     ["р"] = "h", ["о"] = "j", ["л"] = "k", ["д"] = "l", ["ж"] = ";", ["э"] = "'",
     ["я"] = "z", ["ч"] = "x", ["с"] = "c", ["м"] = "v", ["и"] = "b", ["т"] = "n",
     ["ь"] = "m", ["б"] = ",", ["ю"] = ".",
     ["Ё"] = "~", ["Й"] = "Q", ["Ц"] = "W", ["У"] = "E", ["К"] = "R", ["Е"] = "T",
     ["Н"] = "Y", ["Г"] = "U", ["Ш"] = "I", ["Щ"] = "O", ["З"] = "P", ["Х"] = "{",
     ["Ъ"] = "}", ["Ф"] = "A", ["Ы"] = "S", ["В"] = "D", ["А"] = "F", ["П"] = "G",
     ["Р"] = "H", ["О"] = "J", ["Л"] = "K", ["Д"] = "L", ["Ж"] = ":", ["Э"] = "\"",
     ["Я"] = "Z", ["Ч"] = "X", ["С"] = "C", ["М"] = "V", ["И"] = "B", ["Т"] = "N",
     ["Ь"] = "M", ["Б"] = "<", ["Ю"] = ">"},
    -- Hebrew (Shift types Latin capitals there already)
    {["ק"] = "e", ["ר"] = "r", ["א"] = "t", ["ט"] = "y", ["ו"] = "u", ["ן"] = "i",
     ["ם"] = "o", ["פ"] = "p", ["ש"] = "a", ["ד"] = "s", ["ג"] = "d", ["כ"] = "f",
     ["ע"] = "g", ["י"] = "h", ["ח"] = "j", ["ל"] = "k", ["ך"] = "l", ["ף"] = ";",
     ["ז"] = "z", ["ס"] = "x", ["ב"] = "c", ["ה"] = "v", ["נ"] = "b", ["מ"] = "n",
     ["צ"] = "m", ["ת"] = ",", ["ץ"] = "."},
}
local MODIFIERS = {"", "Ctrl+", "Alt+", "Ctrl+Alt+"}

local lines = {}
for _, layout in ipairs(LAYOUTS) do
    for char, key in pairs(layout) do
        for _, mod in ipairs(MODIFIERS) do
            -- "keypress" goes through the normal input path, so whatever owns the
            -- Latin key -- input.conf, a default binding, a script -- gets it.
            -- Quoted: ";" would otherwise end the command.
            local target = (mod .. key):gsub("\\", "\\\\"):gsub("\"", "\\\"")
            lines[#lines + 1] = string.format('%s%s keypress "%s"', mod, char, target)
        end
    end
end
-- One weak ("default") section instead of hundreds of script bindings.
mp.commandv("define-section", "key-layouts", table.concat(lines, "\n"), "default")
mp.commandv("enable-section", "key-layouts")
