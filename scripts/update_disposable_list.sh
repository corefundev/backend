#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────
# update_disposable_list.sh
#
# Fetches the curated disposable-email-domains list from
# github.com/disposable/disposable-email-domains and updates
# configs/disposable_domains.txt.
#
# We keep a header comment block in our file pointing to docs and the
# audit reasons; the script preserves those by appending the upstream
# entries to the existing curated header.
#
# Run periodically (monthly?). The list grows ~5-10 entries per week.
#
# Audit step (manual):
# After running, `git diff configs/disposable_domains.txt` and skim
# for legitimate providers that may have been added upstream by mistake
# (e.g. a major regional ISP misclassified). If you spot one, comment
# it out before committing.
# ─────────────────────────────────────────────────────────────────────────
set -eu

UPSTREAM="https://raw.githubusercontent.com/disposable/disposable-email-domains/master/domains.txt"
TARGET="configs/disposable_domains.txt"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

if [ ! -f "$TARGET" ]; then
    echo "ERROR: $TARGET not found — run from backend/ root" >&2
    exit 2
fi

echo "[update_disposable] downloading upstream list…"
curl -fsSL --max-time 30 "$UPSTREAM" -o "$TMP"
CNT=$(wc -l < "$TMP" | tr -d ' ')
echo "[update_disposable] upstream has $CNT entries"

# Header preserved from current file (everything before the first
# non-comment, non-blank line).
HEADER_END=$(awk 'NR > 1 && !/^#/ && !/^$/ { print NR-1; exit }' "$TARGET")
if [ -z "$HEADER_END" ]; then
    HEADER_END=$(wc -l < "$TARGET")
fi

{
    head -n "$HEADER_END" "$TARGET"
    echo ""
    echo "# ── Imported from upstream $(date -u +%Y-%m-%d) ──"
    echo "# https://github.com/disposable/disposable-email-domains"
    echo ""
    # Skip empty lines, lowercase, keep unique.
    grep -v '^[[:space:]]*$' "$TMP" | tr '[:upper:]' '[:lower:]' | sort -u
} > "$TARGET.new"

mv "$TARGET.new" "$TARGET"
echo "[update_disposable] $TARGET updated."
echo ""
echo "Now run:  git diff $TARGET   — audit before committing."
