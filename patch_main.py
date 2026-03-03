"""
CFC Orders - main.py Patch Script (Session 16)
Applies 3 edits:
  1. startup_wiring import after alerts_router mount
  2. Freight class 70 -> 85 (3 spots)
  3. root() endpoint: add lifecycle/email/ai_configure status

Run from: C:\dev\CFCOrderBackend_Sandbox
Command:  python patch_main.py
"""
import re
import shutil
from pathlib import Path

FILE = Path("main.py")

if not FILE.exists():
    print("ERROR: main.py not found. Run this from C:\\dev\\CFCOrderBackend_Sandbox")
    raise SystemExit(1)

# Backup
backup = FILE.with_suffix(".py.bak")
shutil.copy2(FILE, backup)
print(f"Backup created: {backup}")

text = FILE.read_text(encoding="utf-8")
original = text  # keep for comparison

# ── EDIT 1: Add startup_wiring import after alerts_router mount ──
EDIT1_FIND = """    app.include_router(alerts_router)"""
EDIT1_REPLACE = """    app.include_router(alerts_router)

# Phase 3B+4: Lifecycle + Email + AI Config (one-call wiring)
from startup_wiring import wire_all
WIRING_STATUS = wire_all(app)"""

count1 = text.count(EDIT1_FIND)
if count1 != 1:
    print(f"ERROR EDIT 1: Expected 1 match for alerts_router block, found {count1}")
    raise SystemExit(1)
text = text.replace(EDIT1_FIND, EDIT1_REPLACE, 1)
print("EDIT 1 applied: startup_wiring import added after alerts_router mount")

# ── EDIT 2: Freight class 70 -> 85 (3 spots) ──
# Spot 1 & 2: default parameter values
OLD_DEFAULT = 'freight_class: str = "70"'
NEW_DEFAULT = 'freight_class: str = "85"'
count2a = text.count(OLD_DEFAULT)
if count2a != 2:
    print(f"ERROR EDIT 2a: Expected 2 default matches, found {count2a}")
    raise SystemExit(1)
text = text.replace(OLD_DEFAULT, NEW_DEFAULT)
print(f"EDIT 2a applied: freight_class default 70->85 ({count2a} spots)")

# Spot 3: hardcoded value
OLD_HARD = 'freight_class="70",'
NEW_HARD = 'freight_class="85",'
count2b = text.count(OLD_HARD)
if count2b != 1:
    print(f"ERROR EDIT 2b: Expected 1 hardcoded match, found {count2b}")
    raise SystemExit(1)
text = text.replace(OLD_HARD, NEW_HARD, 1)
print("EDIT 2b applied: freight_class hardcoded 70->85 (1 spot)")

# ── EDIT 3: root() endpoint - add lifecycle/email/ai_configure status ──
EDIT3_FIND = '''        "alerts_engine": {
            "enabled": ALERTS_ENGINE_LOADED
        }
    }'''

EDIT3_REPLACE = '''        "alerts_engine": {
            "enabled": ALERTS_ENGINE_LOADED
        },
        "lifecycle_engine": {
            "enabled": WIRING_STATUS.get("lifecycle", False)
        },
        "email_engine": {
            "enabled": WIRING_STATUS.get("email", False)
        },
        "ai_configure": {
            "enabled": WIRING_STATUS.get("ai_configure", False)
        }
    }'''

count3 = text.count(EDIT3_FIND)
if count3 != 1:
    print(f"ERROR EDIT 3: Expected 1 match for alerts_engine block, found {count3}")
    print("Trying with flexible whitespace...")
    # Try regex for flexible whitespace
    pattern = r'("alerts_engine":\s*\{\s*"enabled":\s*ALERTS_ENGINE_LOADED\s*\}\s*\})'
    match = re.search(pattern, text)
    if match:
        text = text[:match.start()] + EDIT3_REPLACE.lstrip() + text[match.end():]
        print("EDIT 3 applied via regex fallback")
    else:
        print("ERROR EDIT 3: Could not find alerts_engine block even with regex")
        raise SystemExit(1)
else:
    text = text.replace(EDIT3_FIND, EDIT3_REPLACE, 1)
    print("EDIT 3 applied: lifecycle/email/ai_configure added to root() return")

# ── Verify no remaining freight_class="70" ──
remaining_70 = text.count('freight_class="70"') + text.count('freight_class: str = "70"')
if remaining_70 > 0:
    print(f"WARNING: {remaining_70} freight_class='70' references still remain!")
else:
    print("Verification: zero freight_class='70' references remain ✓")

# ── Write ──
FILE.write_text(text, encoding="utf-8")
lines_changed = sum(1 for a, b in zip(original.splitlines(), text.splitlines()) if a != b)
new_lines = len(text.splitlines()) - len(original.splitlines())
print(f"\nmain.py updated: +{new_lines} net new lines")
print(f"Backup at: {backup}")
print("\nNext steps:")
print("  1. git add main.py")
print("  2. git commit -m 'Wire startup_wiring + freight 85 + root status'")
print("  3. git push")
print("  4. Wait ~2-3 min for Render redeploy")
print("  5. POST /add-lifecycle-fields")
print("  6. POST /backfill-lifecycle")
