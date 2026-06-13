# Run from an EMPTY directory (demo refuses a populated db), then move demo.gif into docs/:
#   cd "$(mktemp -d)" && vhs /path/to/ledgerline/docs/demo.tape && mv demo.gif /path/to/ledgerline/docs/
Output "demo.gif"

Set Shell bash
Set FontSize 16
Set Width 1150
Set Height 780
Set Padding 16
Set TypingSpeed 35ms

Type "uvx --from ledgerline ledgerline demo"
Enter
Sleep 8s

Type "clear"
Enter
Type "uvx --from ledgerline ledgerline summary"
Enter
Sleep 7s

Type "clear"
Enter
Type "uvx --from ledgerline ledgerline upcoming"
Enter
Sleep 7s

Sleep 2s
