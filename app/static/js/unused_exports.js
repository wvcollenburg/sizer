// PARKED (unused) client-side export functions.
//
// These drove the per-recommendation, per-config and in-sizer multi-site export
// buttons that were REMOVED when exports moved to the project level. They call
// the synchronous /api/export-* endpoints, which are likewise parked server-side
// in app/unused_exports.py.
//
// This file is NOT loaded by any template, so none of it runs. It is kept only
// so the behaviour can be restored without digging through git history: re-add
// the buttons, re-register the server routes (see unused_exports.py), and load
// this script from index.html. `esc`, `canExportEditable`, `window.t`,
// `toastError`, `lastRecommendations`, `separateClusters`, etc. are provided by
// app.js/auth.js when this runs in that context.
/* eslint-disable */

const _EXPORT_ENDPOINTS = {
    pptx: '/api/export-proposal',
    pdf: '/api/export-pdf',
    docx: '/api/export-docx',
    'presentation-pdf': '/api/export-presentation-pdf',
};


async function exportProposal(mode, recIndex, fmt = 'pptx') {
    const recs = lastRecommendations[mode];
    const summary = lastSummary[mode];
    const projection = lastProjection[mode];

    if (!recs || !recs[recIndex] || !summary || !projection) {
        toastError(window.t('results.export_missing_data'));
        return;
    }

    const btn = (event.target.closest && event.target.closest('button')) || event.target;
    const origHtml = btn.innerHTML;
    btn.textContent = window.t('results.generating');
    btn.disabled = true;

    // With a single-mode DR cluster configured, export one combined document
    // covering the primary workload AND its DR target (reuses the multi-site
    // builder). Otherwise export the single recommendation as before.
    const drExport = !separateClusters && drCluster.enabled
        && lastDrResult && lastDrResult.recommendations && lastDrResult.recommendations.length
        && (mode === 'import' || mode === 'manual');

    try {
        let resp, fallbackName;
        if (drExport) {
            const clusters = [
                { name: window.t('cluster.dr_tab_primary'), summary,
                  recommendation: recs[recIndex], projection, source_perf: buildSourcePerfExport(),
                  replicates_to: window.t('cluster.dr_tab_dr') },
                { name: window.t('cluster.dr_tab_dr'), summary: lastDrResult.summary,
                  recommendation: lastDrResult.recommendations[0], projection: lastDrResult.projection,
                  source_perf: null, replicates_to: '' },
            ];
            resp = await fetch(_MULTISITE_ENDPOINTS[fmt] || _MULTISITE_ENDPOINTS.pptx, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ clusters }),
            });
            fallbackName = `SC_Proposal_Primary_plus_DR.${fmt}`;
        } else {
            resp = await fetch(_EXPORT_ENDPOINTS[fmt] || _EXPORT_ENDPOINTS.pptx, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    summary: summary,
                    recommendation: recs[recIndex],
                    projection: projection,
                    source_perf: buildSourcePerfExport(),
                }),
            });
            fallbackName = `SC_Proposal_${recs[recIndex].model}_${recs[recIndex].node_count}N.${fmt}`;
        }

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            toastError(err.error || window.t('results.export_failed'));
            return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1] || fallbackName;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        toastError(window.t('results.export_failed_detail', {error: e.message}));
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
    }
}


const _MULTISITE_ENDPOINTS = {
    pptx: '/api/export-bundle-proposal',
    docx: '/api/export-bundle-docx',
    pdf: '/api/export-bundle-pdf',
    'presentation-pdf': '/api/export-bundle-presentation-pdf',
};


async function exportMultisite(fmt = 'pptx') {
    if (!separateClusters) return;
    const btn = (event && event.target.closest && event.target.closest('button')) || (event && event.target);
    const origHtml = btn && btn.innerHTML;
    if (btn) { btn.textContent = window.t('results.generating'); btn.disabled = true; }
    try {
        await ensureAllClusterResults();
        const payloadClusters = sourceClusters.map(c => {
            const res = clusterResults[c.name];
            if (!res || !res.recommendations || !res.recommendations.length) return null;
            // Use the cluster's chosen recommendation (defaults to #1), clamped
            // in case a re-size shortened the list.
            const sel = Math.min(clusterSelectedRec[c.name] ?? 0, res.recommendations.length - 1);
            const target = (clusterReplication[c.name] || {}).target || '';
            return {
                name: c.name,
                summary: res.summary,
                recommendation: res.recommendations[sel],
                projection: res.projection,
                source_perf: null,
                replicates_to: target ? clusterDisplayName(target) : '',
            };
        }).filter(Boolean);

        if (!payloadClusters.length) {
            toastError(window.t('results.export_missing_data'));
            return;
        }

        const resp = await fetch(_MULTISITE_ENDPOINTS[fmt] || _MULTISITE_ENDPOINTS.pptx, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ clusters: payloadClusters }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            toastError(err.error || window.t('results.export_failed'));
            return;
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Proposal_MultiSite.${fmt}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        toastError(window.t('results.export_failed_detail', {error: e.message}));
    } finally {
        if (btn) { btn.innerHTML = origHtml; btn.disabled = false; }
    }
}


async function exportConfig(fmt = 'pptx') {
    if (!lastConfigResult) {
        toastError(window.t('results.no_config_to_export'));
        return;
    }

    const endpoint = fmt === 'pdf' ? '/api/export-config-pdf' : '/api/export-config';
    const btn = (event && event.target.closest && event.target.closest('button'))
        || document.getElementById('config-export-btn');
    const origHtml = btn.innerHTML;
    btn.textContent = window.t('results.generating');
    btn.disabled = true;

    try {
        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(lastConfigResult),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            toastError(err.error || window.t('results.export_failed'));
            return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Config_${lastConfigResult.node_count}N.${fmt}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        toastError(window.t('results.export_failed_detail', {error: e.message}));
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
    }
}


// From wizard.js (guided-wizard export step):
window.wizExport = function (fmt) {
    if (typeof exportProposal === 'function') exportProposal('import', 0, fmt);
};

