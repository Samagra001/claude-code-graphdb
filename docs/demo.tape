# VHS tape: graphdb demo against smart-hub-backend
# Generate with: vhs docs/demo.tape  ->  docs/demo.gif
# Install vhs:   brew install vhs

Output docs/demo.gif

Set FontSize 13
Set Width 1280
Set Height 760
Set Theme "Catppuccin Mocha"
Set TypingSpeed 25ms
Set PlaybackSpeed 1.0
Set Padding 20
Set Framerate 24

# Setup — hidden from the final GIF so commands look clean.
# GRAPHDB_CACHE_DIR points at a snapshot of the graph so the recording
# doesn't fight the running MCP server's lock on ~/.cache/graphdb/.
Hide
Type "export PATH=/Users/samagrapathak/Desktop/Graph_DB/.venv/bin:$PATH"
Enter
Sleep 200ms
Type "export GRAPHDB_CACHE_DIR=/tmp/graphdb-demo"
Enter
Sleep 200ms
Type "cd ~"
Enter
Sleep 200ms
Type "clear"
Enter
Sleep 200ms
Show

# ──────────────────────────────────────────────────────────────
# Scene 1: graph health check
# ──────────────────────────────────────────────────────────────
Type "# A Ruby-on-Rails codebase, fully indexed in a single file:"
Enter
Sleep 1500ms
Type "graphdb --root ~/Desktop/smart-hub-backend stats | head -15"
Sleep 400ms
Enter
Sleep 3500ms

# ──────────────────────────────────────────────────────────────
# Scene 2: routes for a controller (the killer demo)
# ──────────────────────────────────────────────────────────────
Type "clear"
Enter
Sleep 300ms
Type "# What URLs hit Api::V1::CampaignsController?"
Enter
Sleep 1200ms
Type "graphdb --root ~/Desktop/smart-hub-backend query routes Api::V1::CampaignsController"
Sleep 400ms
Enter
Sleep 4500ms

# ──────────────────────────────────────────────────────────────
# Scene 3: model associations
# ──────────────────────────────────────────────────────────────
Type "clear"
Enter
Sleep 300ms
Type "# All ActiveRecord associations on User (forward + reverse)"
Enter
Sleep 1200ms
Type "graphdb --root ~/Desktop/smart-hub-backend query associations User | head -35"
Sleep 400ms
Enter
Sleep 4500ms

# ──────────────────────────────────────────────────────────────
# Scene 4: who calls / delivers / enqueues
# ──────────────────────────────────────────────────────────────
Type "clear"
Enter
Sleep 300ms
Type "# Who delivers UserInviteMailer? (2 hops, CALLS ∪ ENQUEUES ∪ DELIVERS)"
Enter
Sleep 1200ms
Type "graphdb --root ~/Desktop/smart-hub-backend query callers UserInviteMailer --depth 2 | head -30"
Sleep 400ms
Enter
Sleep 4500ms

# ──────────────────────────────────────────────────────────────
# Scene 5: blast radius
# ──────────────────────────────────────────────────────────────
Type "clear"
Enter
Sleep 300ms
Type "# What breaks if we change ScheduleExecutionJob?"
Enter
Sleep 1200ms
Type "graphdb --root ~/Desktop/smart-hub-backend query impact ScheduleExecutionJob --max-depth 3"
Sleep 400ms
Enter
Sleep 4500ms

# ──────────────────────────────────────────────────────────────
# Closing
# ──────────────────────────────────────────────────────────────
Type "clear"
Enter
Sleep 300ms
Type "# All 8 tools also exposed to Claude Code via MCP. ~3x fewer tokens than grep."
Enter
Sleep 3000ms
