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
echo "==> [ 1/21] DIP legislative pipeline (Bund, intraday)"
python3 pipeline/fetch_dip.py || {
    echo "[warn] DIP refresh degraded — watch state will not be advanced"
    WATCH_SOURCES_OK=0
}
echo "==> [ 2/21] explicit EUR-Lex procedure watches"
python3 pipeline/fetch_eu_watch.py || {
    echo "[warn] EUR-Lex watch refresh degraded — watch state will not be advanced"
    WATCH_SOURCES_OK=0
}
if [ "$WATCH_SOURCES_OK" -eq 1 ]; then
    step " 3/21" "persistent procedure state + change-only history" \
        python3 tools/update_procedure_watch.py
else
    echo "==> [ 3/21] watch state skipped (one official refresh failed)"
fi
step " 4/21" "BGBl promulgation events (daily feed)"       python3 pipeline/fetch_bgbl_events.py
step " 5/21" "GII corpus HEAD (lags days-weeks)"           python3 pipeline/fetch_gii.py
step " 6/21" "Federal case law (official RII feeds)"       python3 pipeline/fetch_rii.py
step " 7/21" "NeuRIS changelog (append-only archive)"      python3 pipeline/fetch_neuris_changelog.py

echo "==> [ 8/21] buzer back-history (max once per day — private site)"
if [ -d "data/snapshots/buzer/$(date +%F)" ]; then
    echo "    today's snapshot exists, skipping"
else
    python3 pipeline/fetch_buzer.py || echo "[warn] step degraded — continuing"
fi

# extract first: it writes the DIP text cache that br_texts scans for
# cover letters; if br_texts then fetched NEW substantive texts, run the
# extraction again so BR patches land in THIS cycle, not the next
step " 9/21" "PatchInstruction extraction (writes dip_text cache)" python3 pipeline/extract_patches.py
BRLOG="$(mktemp)"
echo "==> [10/21] Bundesrat texts (cache-first; 30s crawl-delay for new)"
python3 pipeline/fetch_br_texts.py | tee "$BRLOG" || echo "[warn] step degraded — continuing"
if grep -q "requests=0 " "$BRLOG"; then
    echo "==> [11/21] re-extraction skipped (no new BR texts)"
else
    step "11/21" "PatchInstruction re-extraction (absorb new BR texts)" python3 pipeline/extract_patches.py
fi
rm -f "$BRLOG"

step "12/21" "BAYERN.RECHT corpus HEAD + BayRS chains"     python3 pipeline/fetch_bayern_recht.py
step "13/21" "GVBl/BayMBl promulgation events (RSS)"       python3 pipeline/fetch_gvbl_events.py
step "14/21" "Bayerischer Landtag WP19 pipeline"           python3 pipeline/fetch_bay_landtag.py
step "15/21" "EU layer: curated corpus + transpositions"   python3 pipeline/fetch_eu_layer.py
step "16/21" "EU breadth index (all directives + basic regulations)" python3 pipeline/fetch_eu_index.py
step "17/21" "Länder monitor (Parlamentsspiegel, Asyl/Sozial)" python3 pipeline/fetch_parlamentsspiegel.py
step "18/21" "Länder-Gesetzentwürfe (alle 16 Landtage)"    python3 pipeline/fetch_laender_bills.py

echo "==> [19/21] build arena"
python3 tools/build_qfs.py
VIS="/home/echo0x22/Documents/Projects/03_PROJECTS_HOBBY/qfs_visualizer/public"
[ -d "$VIS" ] && cp data/lexgraph_de_wp21.qfs "$VIS/" && echo "deployed to qfs_visualizer"

echo "==> [20/21] export web data (Wiki/Realtime/Hierarchie/Graph)"
python3 tools/build_web_data.py

step "21/21" "LLM digest (skips without OPENROUTER_API_KEY)" python3 tools/build_digest.py
echo "OK — serve with: python3 -m http.server -d web 8777"
