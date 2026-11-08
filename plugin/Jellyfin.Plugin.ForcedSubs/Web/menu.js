/*
 * Forced Subtitles plugin: adds "Generate subtitles" to Jellyfin item menus (the "..." menu
 * on an item page and on library cards). Shown to administrators, for items that have
 * "Edit subtitles" (i.e. videos). Uses the settings from the plugin's dashboard page.
 */
(function () {
    'use strict';
    if (window.__forcedSubsMenu) return;
    window.__forcedSubsMenu = true;

    var MENU_ID = 'forcedsubs-generate';
    var lastItemId = null;
    var isAdmin = null;

    function api() { return window.ApiClient; }

    function checkAdmin() {
        if (isAdmin !== null || !api()) return;
        isAdmin = false;
        api().getCurrentUser().then(function (u) {
            isAdmin = !!(u && u.Policy && u.Policy.IsAdministrator);
        }, function () { isAdmin = null; });
    }

    function idFromUrl() {
        var q = (location.hash.split('?')[1] || location.search.slice(1));
        var id = new URLSearchParams(q).get('id');
        return id ? id.replace(/-/g, '') : null;
    }

    // Remember which item a menu is being opened for.
    function rememberSource(e) {
        checkAdmin();
        var t = e.target;
        if (!t || !t.closest) return;
        if (t.closest('.btnMoreCommands')) { lastItemId = idFromUrl(); return; }
        var card = t.closest('[data-id][data-type], .card[data-id], .listItem[data-id]');
        if (card && !card.classList.contains('actionSheetMenuItem')) {
            lastItemId = card.getAttribute('data-id');
        }
    }
    document.addEventListener('pointerdown', rememberSource, true);
    document.addEventListener('contextmenu', rememberSource, true);
    document.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') rememberSource(e); }, true);

    // ------------------------------------------------------------------ toast
    var toastBox = null;
    function toast(text, kind, ms) {
        if (!toastBox) {
            toastBox = document.createElement('div');
            toastBox.style.cssText = 'position:fixed;left:50%;bottom:2em;transform:translateX(-50%);z-index:99999;' +
                'display:flex;flex-direction:column;gap:.5em;align-items:center;pointer-events:none;';
            document.body.appendChild(toastBox);
        }
        var el = document.createElement('div');
        var bg = kind === 'error' ? '#b3261e' : kind === 'done' ? '#2e7d32' : '#303030';
        el.style.cssText = 'background:' + bg + ';color:#fff;padding:.8em 1.2em;border-radius:.4em;max-width:36em;' +
            'box-shadow:0 2px 8px rgba(0,0,0,.4);font-size:.95em;line-height:1.4;opacity:0;transition:opacity .2s;';
        el.textContent = text;
        toastBox.appendChild(el);
        requestAnimationFrame(function () { el.style.opacity = '1'; });
        setTimeout(function () {
            el.style.opacity = '0';
            setTimeout(function () { el.remove(); }, 300);
        }, ms || 5000);
    }

    // ------------------------------------------------------------------ action
    function call(method, path, params) {
        return api().ajax({ type: method, url: api().getUrl(path, params || {}), dataType: 'json' });
    }

    function watchJob(jobId, name, startedAt) {
        setTimeout(function () {
            call('GET', 'ForcedSubs/Jobs', { limit: 100 }).then(function (jobs) {
                var job = (jobs || []).filter(function (j) { return j.Id === jobId; })[0];
                if (!job) return; // fell out of the list; the dashboard page has the history
                if (job.Status === 'queued' || job.Status === 'running') {
                    if (Date.now() - startedAt < 4 * 3600 * 1000) watchJob(jobId, name, startedAt);
                    return;
                }
                // Keep the notice short: "Wrote en.forced (10), es (21)" without the list of skipped files.
                var summary = (job.Message || '').split('. ')[0];
                if (job.Status === 'done') toast('Subtitles ready for ' + name + '. ' + summary + '.', 'done', 9000);
                else if (job.Status === 'skipped') toast(name + ': ' + (job.Message || 'nothing to do'), null, 9000);
                else if (job.Status === 'failed') toast('Subtitles failed for ' + name + ': ' + (job.Message || ''), 'error', 12000);
            }, function () { watchJob(jobId, name, startedAt); });
        }, 5000);
    }

    function generate(itemId) {
        if (!api()) return;
        api().getItem(api().getCurrentUserId(), itemId).then(function (item) {
            return item.Name || 'this item';
        }, function () { return 'this item'; }).then(function (name) {
            call('POST', 'ForcedSubs/Generate/' + itemId).then(function (job) {
                toast('Generating subtitles for ' + name + '. You’ll get a notice here when they’re ready.', null, 6000);
                if (job && job.id) watchJob(job.id, name, Date.now());
            }, function () {
                toast('Couldn’t start subtitles for ' + name + '. Is the subtitle service running? ' +
                    'Check Dashboard > Plugins > Forced Subtitles.', 'error', 10000);
            });
        });
    }

    // ------------------------------------------------------------------ menu
    function addEntry(sheet) {
        if (sheet.querySelector('[data-id="' + MENU_ID + '"]')) return;
        var anchor = sheet.querySelector('.actionSheetMenuItem[data-id="editsubtitles"]');
        if (!anchor || !isAdmin) return;
        var itemId = lastItemId || idFromUrl();
        if (!itemId) return;

        var btn = anchor.cloneNode(true);
        btn.setAttribute('data-id', MENU_ID);
        var icon = btn.querySelector('.material-icons');
        if (icon) icon.className = icon.className.replace(/\bclosed_caption\b/, '') + ' subtitles';
        var label = btn.querySelector('.actionSheetItemText');
        if (label) label.textContent = 'Generate subtitles';
        btn.addEventListener('click', function () {
            // The menu closes itself (the unknown command is ignored); start the job.
            generate(itemId);
        });
        anchor.parentNode.insertBefore(btn, anchor.nextSibling);
    }

    new MutationObserver(function (mutations) {
        for (var i = 0; i < mutations.length; i++) {
            var added = mutations[i].addedNodes;
            for (var j = 0; j < added.length; j++) {
                var n = added[j];
                if (n.nodeType !== 1) continue;
                var sheet = n.classList.contains('actionSheet') ? n : n.querySelector && n.querySelector('.actionSheet');
                if (sheet) addEntry(sheet);
            }
        }
    }).observe(document.body, { childList: true, subtree: true });

    checkAdmin();
})();
