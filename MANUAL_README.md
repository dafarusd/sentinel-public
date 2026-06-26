# README — How to Read the Sentinel Operator's Manual

**Companion file for:** `OPERATOR_MANUAL.md`
**Purpose:** Teaches you how to navigate, search, and read the operator's manual on the Pi (or Framework) without an editor.
**Who this is for:** You, when you SSH into the Pi and want to find something in the manual fast.

The manual is 86 KB of dense reference material — 2,962 lines across 26 sections. You should almost never read it top-to-bottom. Instead, use the tools below to jump exactly where you need to be.

---

## The Two Commands That Matter

Two Unix utilities do 99% of the work:

- **`less`** — opens a file for reading, lets you scroll and search
- **`grep`** — searches a file and prints matching lines, without opening it

You don't need any editor. You don't need a GUI. Both ship with every Linux box.

---

## `less` — The Pager

`less` is a read-only viewer. It loads the file, lets you scroll and search, and exits cleanly. It never modifies the file. Safe for anything.

### Opening the manual

```bash
less ~/sentinel/OPERATOR_MANUAL.md
```

### Keyboard controls inside `less`

Once the file is open, these keys work:

| Key | What it does |
|---|---|
| `Space` or `Page Down` | Next screen |
| `b` or `Page Up` | Previous screen |
| `↓` / `↑` | Line-by-line scroll |
| `g` | Jump to the top |
| `G` (shift+g) | Jump to the bottom |
| `/word` + Enter | Search forward for "word" |
| `?word` + Enter | Search backward for "word" |
| `n` | Next search match |
| `N` (shift+n) | Previous search match |
| `h` | Show all help |
| `q` | Quit |

### Open directly to a section

Instead of opening the file and then scrolling, you can tell `less` to jump somewhere on launch. Use `+'/pattern'` right after `less`:

```bash
less +'/Playbook' ~/sentinel/OPERATOR_MANUAL.md
```

That opens the manual and jumps to the first line containing "Playbook" — which lands you in Part IV, the investigation scenarios.

More examples:

```bash
less +'/Ferrari' ~/sentinel/OPERATOR_MANUAL.md
# → Jumps to Part III, the advanced query library

less +'/Emergency' ~/sentinel/OPERATOR_MANUAL.md
# → Jumps to Part VI, Section 24, emergency commands

less +'/Quick-Reference' ~/sentinel/OPERATOR_MANUAL.md
# → Jumps to Section 25, the 20 most-used commands card

less +'/Troubleshooting' ~/sentinel/OPERATOR_MANUAL.md
# → Jumps to Section 22

less +'/Dictionary' ~/sentinel/OPERATOR_MANUAL.md
# → Jumps to Section 23, all the reference tables
```

The word in quotes can be anything. If the first match isn't what you wanted, press `n` to jump to the next occurrence.

### Open at a specific line number

If you know the line number of what you want (see `grep` below):

```bash
less +542 ~/sentinel/OPERATOR_MANUAL.md
```

Opens the file with line 542 at the top of the screen.

---

## `grep` — Instant Search Without Opening

`grep` searches a file for a pattern and prints only the matching lines. Great for finding **all** the places a topic appears in the manual, or for generating a table of contents.

### The two flags you'll use most

- **`-n`** — include line numbers in output
- **`-i`** — case-insensitive match (so "Backup" matches "backup", "BACKUP")

### List every section heading (your table of contents)

```bash
grep -n "^## " ~/sentinel/OPERATOR_MANUAL.md
```

**What it does:**
- `"^## "` is a pattern: `^` means "start of line," so this matches lines starting with `## ` — which is how markdown section headings look.
- `-n` adds the line number.

**Output looks like:**
```
42:## 1. Architecture and Mental Model
187:## 2. The Full Data Model
...
2604:## 25. Quick-Reference Card
```

Now you have every section and its line number. Pick one and open the manual there:

```bash
less +1542 ~/sentinel/OPERATOR_MANUAL.md
```

### List all playbooks specifically

```bash
grep -n "^## Playbook" ~/sentinel/OPERATOR_MANUAL.md
```

### Find a specific topic everywhere it's mentioned

```bash
grep -n -i "probe cluster" ~/sentinel/OPERATOR_MANUAL.md
```

**What it does:**
- Finds every line with "probe cluster" (case-insensitive)
- Prints line numbers
- Use the line numbers to jump into `less` at that spot

More examples:

```bash
grep -n -i "backup" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "vacuum" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "wlan0" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "apple" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "rssi" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "systemd" ~/sentinel/OPERATOR_MANUAL.md
```

### Find SQL query examples

Every runnable query in the manual lives inside a `bash` fenced block. To find command blocks:

```bash
grep -n "^```bash" ~/sentinel/OPERATOR_MANUAL.md | head -20
```

Gives you line numbers of the first 20 code blocks.

---

## The Three Workflows

### Workflow 1: "Just let me read this section"

You already know what you're looking for.

```bash
less +'/Troubleshooting' ~/sentinel/OPERATOR_MANUAL.md
```

Scroll with Space. Quit with `q`.

### Workflow 2: "Show me the whole menu, then let me pick"

Start with the table of contents:

```bash
grep -n "^## " ~/sentinel/OPERATOR_MANUAL.md
```

Read the output. Pick the section you want. Note its line number. Then:

```bash
less +<line_number> ~/sentinel/OPERATOR_MANUAL.md
```

Example:
```bash
less +1823 ~/sentinel/OPERATOR_MANUAL.md
```

### Workflow 3: "I need every place this topic is mentioned"

```bash
grep -n -i "your_topic" ~/sentinel/OPERATOR_MANUAL.md
```

Review the hits. Open the most relevant line number in `less`.

---

## Bonus: Read from the Framework Without SSH'ing

If you're on the Framework and want to browse the manual there, same commands, different path:

```bash
less ~/projects/sentinel/OPERATOR_MANUAL.md
grep -n "^## " ~/projects/sentinel/OPERATOR_MANUAL.md
```

---

## Bonus: Read from the Framework Over SSH

Run a grep on the Pi without opening a shell session:

```bash
ssh user@192.168.1.100 "grep -n '^## ' ~/sentinel/OPERATOR_MANUAL.md"
```

Or pipe sections back:

```bash
ssh user@192.168.1.100 "sed -n '1823,1900p' ~/sentinel/OPERATOR_MANUAL.md" | less
```

(`sed -n '1823,1900p'` prints lines 1823-1900 from the file.)

---

## Why Not Use an Editor?

You could open the manual with `nano ~/sentinel/OPERATOR_MANUAL.md` but that:

1. Loads the whole file into an editor — slower for 86 KB
2. Risks accidental edits — one stray keystroke and your manual is corrupted
3. Has no search-and-jump-on-open feature
4. Doesn't scroll smoothly on long files

`less` is purpose-built for **reading long text**. It's what every Linux sysadmin uses for logs, man pages, config files, and manuals. It's also what `man` uses internally — when you type `man ssh`, you're using `less` with the man page loaded.

---

## Quick-Reference Card for This README

Copy-paste these when you need them:

```bash
# Open manual
less ~/sentinel/OPERATOR_MANUAL.md

# Jump to specific section keyword
less +'/Playbook' ~/sentinel/OPERATOR_MANUAL.md
less +'/Emergency' ~/sentinel/OPERATOR_MANUAL.md
less +'/Ferrari' ~/sentinel/OPERATOR_MANUAL.md
less +'/Quick-Reference' ~/sentinel/OPERATOR_MANUAL.md

# Jump to specific line number
less +542 ~/sentinel/OPERATOR_MANUAL.md

# Table of contents
grep -n "^## " ~/sentinel/OPERATOR_MANUAL.md

# List all playbooks
grep -n "^## Playbook" ~/sentinel/OPERATOR_MANUAL.md

# Find topic everywhere
grep -n -i "backup" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "probe cluster" ~/sentinel/OPERATOR_MANUAL.md
grep -n -i "wlan0" ~/sentinel/OPERATOR_MANUAL.md

# Inside less:
#   Space/PgDn  = next page
#   b/PgUp      = prev page
#   /word       = search forward
#   ?word       = search backward
#   n           = next match
#   N           = previous match
#   g           = top
#   G           = bottom
#   q           = quit
```

---

## Keep This File Where the Manual Is

Both the manual and this README should live together. Push this README to the Pi the same way you pushed the manual:

On the Framework:

```bash
# Save this file to project dir first (if you downloaded it)
mv ~/Downloads/README.md ~/projects/sentinel/MANUAL_README.md

# Push to Pi
rsync -avz ~/projects/sentinel/MANUAL_README.md user@192.168.1.100:/home/user/sentinel/MANUAL_README.md

# Verify
ssh user@192.168.1.100 "ls -lh ~/sentinel/MANUAL_README.md ~/sentinel/OPERATOR_MANUAL.md"
```

Now both files are on both machines. When you're SSH'd into the Pi and forget how to use `less`:

```bash
less ~/sentinel/MANUAL_README.md
```

Start there.

---

**End of README.**
