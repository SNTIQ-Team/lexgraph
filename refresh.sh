#!/usr/bin/env bash
# Lexgraph realtime refresh: pull the live legislative state across
# Bund / Bayern / EU / Länder and rebuild the arena. Run on cron.
#
# Fetch steps degrade gracefully (a flaky source must not kill the
# arena rebuild); only the final build is fatal. Fetchers refuse to
# overwrite a good same-day snapshot with empty output on their own.
set -euo pipefail
cd "$(dirname "$0")"

step() { echo "==> [$1] $2"; shift 2; "$@" || echo "[warn] step degraded — continuing"; }

WATCH_SOURCES_OK=1
echo "==> [ 1/23] DIP legislative pipeline (Bund, intraday)"
python3 pipeline/fetch_dip.py || {
    echo "[warn] DIP refresh degraded — watch state will not be advanced"
    WATCH_SOURCES_OK=0
}
echo "==> [ 2/23] explicit EUR-Lex procedure watches"
python3 pipeline/fetch_eu_watch.py || {
    echo "[warn] EUR-Lex watch refresh degraded — watch state will not be advanced"
    WATCH_SOURCES_OK=0
}
if [ "$WATCH_SOURCES_OK" -eq 1 ]; then
    step " 3/23" "persistent procedure state + change-only history" \
        python3 tools/update_procedure_watch.py
else
    echo "==> [ 3/23] watch state skipped (one official refresh failed)"
fi
step " 4/23" "BGBl promulgation events (daily feed)"       python3 pipeline/fetch_bgbl_events.py
step " 5/23" "GII corpus HEAD (lags days-weeks)"           python3 pipeline/fetch_gii.py
step " 6/23" "archive complete official GII states"       python3 tools/archive_gii_states.py
step " 7/23" "final BGBl documents + integrity/text"       python3 pipeline/fetch_bgbl_documents.py
echo "==> [ 8/23] Federal case law intake"
if [ "${LEXGRAPH_ENABLE_RII:-0}" = "1" ]; then
    # RII permits reuse of decisions, but its robots/TDM headers conflict
    # with automated ZIP polling.  Keep this legacy intake explicit until
    # it is replaced by the documented NeuRIS bulk/changelog API.
    python3 pipeline/fetch_rii.py || echo "[warn] step degraded — continuing"
else
    echo "    skipped by data policy (use NeuRIS; legacy RII is explicit opt-in)"
fi
step " 9/23" "NeuRIS changelog + expiring ZIP capture"     python3 pipeline/fetch_neuris_changelog.py

echo "==> [10/23] private back-history source"
if [ "${LEXGRAPH_ENABLE_BUZER:-0}" != "1" ]; then
    echo "    skipped: independent official-source history is active; private QA cache is explicit opt-in"
elif [ -d "data/snapshots/buzer/$(date +%F)" ]; then
    echo "    explicit opt-in enabled; today's snapshot exists, skipping"
else
    python3 pipeline/fetch_buzer.py || echo "[warn] step degraded — continuing"
fi

# extract first: it writes the DIP text cache that br_texts scans for
# cover letters; if br_texts then fetched NEW substantive texts, run the
# extraction again so BR patches land in THIS cycle, not the next
step "11/23" "PatchInstruction extraction (writes dip_text cache)" python3 pipeline/extract_patches.py
BRLOG="$(mktemp)"
echo "==> [12/23] Bundesrat texts (cache-first; 30s crawl-delay for new)"
python3 pipeline/fetch_br_texts.py | tee "$BRLOG" || echo "[warn] step degraded — continuing"
if grep -q "requests=0 " "$BRLOG"; then
    echo "==> [13/23] re-extraction skipped (no new BR texts)"
else
    step "13/23" "PatchInstruction re-extraction (absorb new BR texts)" python3 pipeline/extract_patches.py
fi
rm -f "$BRLOG"

step "14/23" "BAYERN.RECHT corpus HEAD + BayRS chains"     python3 pipeline/fetch_bayern_recht.py
step "15/23" "GVBl/BayMBl promulgation events (RSS)"       python3 pipeline/fetch_gvbl_events.py
step "16/23" "Bayerischer Landtag WP19 pipeline"           python3 pipeline/fetch_bay_landtag.py
step "17/23" "EU layer: curated corpus + transpositions"   python3 pipeline/fetch_eu_layer.py
step "18/23" "EU breadth index (all directives + basic regulations)" python3 pipeline/fetch_eu_index.py
echo "==> [19/23] Länder discovery monitor"
if [ "${LEXGRAPH_ENABLE_PARLAMENTSSPIEGEL:-0}" = "1" ]; then
    python3 pipeline/fetch_parlamentsspiegel.py || echo "[warn] step degraded — continuing"
else
    echo "    skipped by data policy (origin-Landtag verification required before publication)"
fi
echo "==> [20/23] broad Länder discovery index"
if [ "${LEXGRAPH_ENABLE_PARLAMENTSSPIEGEL_BULK:-0}" = "1" ]; then
    python3 pipeline/fetch_laender_bills.py || echo "[warn] step degraded — continuing"
else
    echo "    quarantined by data policy (no republication permission for the portal database)"
fi

echo "==> [21/23] build arena"
python3 tools/build_qfs.py
VIS="/home/echo0x22/Documents/Projects/03_PROJECTS_HOBBY/qfs_visualizer/public"
[ -d "$VIS" ] && cp data/lexgraph_de_wp21.qfs "$VIS/" && echo "deployed to qfs_visualizer"

echo "==> [22/23] export web data (Wiki/Realtime/Hierarchie/Graph)"
python3 tools/build_web_data.py

step "23/23" "LLM digest (skips without OPENROUTER_API_KEY)" python3 tools/build_digest.py
echo "OK — serve with: python3 -m http.server -d web 8777"
