# vhs tape for the econ-threat demo GIF (many-to-one enrichment).
# Render with scripts/build_demo_gif.sh scripts/demo_econ_threat.tape econ-threat docs/assets/demo-econ-threat.gif
#
# Each command block ends with `clear` so the next prompt is drawn at the top of
# a clean screen. Pauses come ONLY after output has landed, to give the viewer
# time to read it — never before a command runs (a pre-run pause reads as the
# tool being slow).

Output "demo-econ-threat.gif"

Set Shell bash
Set FontSize 17
Set Width 1200
Set Height 880
Set Padding 16
Set TypingSpeed 35ms
Set Theme "Catppuccin Mocha"

# Quiet bash's interactive mail check so "You have new mail" never appears.
# Unsetting MAILCHECK disables checking entirely; a large value does NOT,
# because bash's first prompt always fires one check (last-checked time starts at 0).
Hide
Type "unset MAIL MAILPATH MAILCHECK; clear"
Enter
Sleep 500ms
Show

Type "doctrail init test econ-threat"
Enter
Sleep 6s
Type "clear"
Enter

Type "ls data/"
Enter
Sleep 3s
Type "clear"
Enter

Type "bat .doctrail/enrichments/econ_threat.yml"
Enter
Sleep 6s
Type "clear"
Enter

Type "doctrail run econ_threat"
Enter
Sleep 4s
Type "clear"
Enter

Type "doctrail view spec econ_threat"
Enter
Sleep 4s
Type "clear"
Enter

# One editorial explodes into many country-editorial rows — the many-to-one.
Set TypingSpeed 10ms
Type `sqlite3 -box out/database.db "SELECT filename, country, econ_threat FROM v_econ_threat ORDER BY filename LIMIT 14"`
Set TypingSpeed 35ms
Enter
Sleep 6s
Type "clear"
Enter

# Linger on one editorial: the Chinese alongside its English translation.
Set TypingSpeed 10ms
Type `sqlite3 -line out/database.db "SELECT substr(raw_content,1,82) AS chinese, substr(english_translation,1,300) AS english FROM v_econ_threat WHERE filename LIKE 'gt_2016%' LIMIT 1"`
Set TypingSpeed 35ms
Enter
Sleep 6s
Type "clear"
Enter

# The model's output: a per-country economic-threat rating with the wrapped quote it keyed on.
Set TypingSpeed 10ms
Type `sqlite3 -cmd ".mode box --wrap 60 --wordwrap on" out/database.db "SELECT country, econ_threat, quote FROM v_econ_threat WHERE filename LIKE 'gt_2016%' ORDER BY econ_threat DESC"`
Set TypingSpeed 35ms
Enter
Sleep 11s
