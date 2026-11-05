// The MCP upload page (templates/mcp_upload.html): one file, posted to the
// upload row (assessments) or to the project's BOM route (BOMs), then the
// row is told what happened so the assistant's get_upload can read it.
// A separate file, not inline: the proxy's CSP is script-src 'self'.
(function () {
    const art = document.querySelector('.mcp-upload');
    if (!art) return;
    const token = art.dataset.token, kind = art.dataset.kind;
    const status = document.getElementById('status');
    const exp = new Date(art.dataset.expires);
    document.getElementById('expires').textContent = isNaN(exp) ? art.dataset.expires
        : exp.toLocaleTimeString();

    function say(text, cls) { status.textContent = text; status.className = 'status ' + (cls || ''); }

    async function report(body) {
        // Tell the upload row what happened (the assistant polls it).
        const r = await fetch('/mcp/upload/' + token + '/done', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)});
        return r.ok;
    }

    document.getElementById('upload-form').addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const input = document.getElementById('file');
        if (!input.files.length) return;
        const fd = new FormData();
        fd.append('file', input.files[0]);
        document.getElementById('submit').disabled = true;
        say('Uploading…');
        try {
            let r;
            if (kind === 'bom') {
                // The BOM goes through the project's own check route (the
                // parsers, the agent path and its budgets, unchanged); the
                // link is then closed with the check or job id.
                if (art.dataset.sizingId) fd.append('sizing_id', art.dataset.sizingId);
                r = await fetch('/api/projects/' + art.dataset.projectId + '/bom-checks',
                                {method: 'POST', body: fd});
                const data = await r.json().catch(() => ({}));
                if (!r.ok) {
                    say(data.error || ('Upload failed (' + r.status + ')'), 'err');
                    document.getElementById('submit').disabled = false;
                    return;
                }
                await report(data.job ? {job_id: data.job.id} : {check_id: data.id});
            } else {
                r = await fetch('/mcp/upload/' + token, {method: 'POST', body: fd});
                const data = await r.json().catch(() => ({}));
                if (!r.ok) {
                    say(data.error || ('Upload failed (' + r.status + ')'), 'err');
                    document.getElementById('submit').disabled = false;
                    return;
                }
            }
            say('Uploaded.', 'ok');
            document.getElementById('done-hint').hidden = false;
            document.getElementById('upload-form').hidden = true;
        } catch (e) {
            say('Upload failed: ' + e.message, 'err');
            document.getElementById('submit').disabled = false;
        }
    });
})();
