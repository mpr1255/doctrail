# vhs tape for the README/docs demo GIF.
# Render with scripts/build_demo_gif.sh — it builds the wheel, puts a
# doctrail venv on PATH, sets DOCTRAIL_INGEST_THROTTLE so per-file ingest
# lines scroll at a readable pace, and runs vhs from a scratch directory.

Output "demo.gif"

Set Shell bash
Set FontSize 17
Set Width 1200
Set Height 720
Set Padding 16
Set TypingSpeed 40ms
Set Theme "Catppuccin Mocha"

Type "doctrail init test fed"
Sleep 600ms
Enter
Sleep 12s

Type "ls data/"
Sleep 400ms
Enter
Sleep 3s

Type "bat .doctrail/enrichments/test.yml"
Sleep 400ms
Enter
Sleep 6s

Type "doctrail run test"
Sleep 500ms
Enter
Sleep 5s

Type "clear"
Sleep 300ms
Enter
Sleep 500ms

Type 'sqlite3 -header -column out/database.db "SELECT filename, author, consensus_author, fear_of_disunion FROM v_documents_enriched ORDER BY fear_of_disunion DESC LIMIT 8"'
Sleep 600ms
Enter
Sleep 8s
