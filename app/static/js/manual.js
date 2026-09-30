// The online user manual (templates/manual/manual.html): highlights the
// chapter in view in the contents, and enlarges a screenshot on click.
(function () {
    // The sticky app header's real height, for the sticky contents and the
    // anchor offsets in manual.css.
    var header = document.querySelector('header');
    function measureHeader() {
        if (header) document.documentElement.style.setProperty(
            '--manual-header-h', header.offsetHeight + 'px');
    }
    measureHeader();
    window.addEventListener('resize', measureHeader);

    var links = Array.prototype.slice.call(document.querySelectorAll('.manual-toc a'));
    var byId = {};
    links.forEach(function (a) { byId[a.getAttribute('href').slice(1)] = a; });

    if ('IntersectionObserver' in window) {
        var visible = {};
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
            // the first chapter (in document order) that is on screen wins
            var current = null;
            document.querySelectorAll('.manual-chapter').forEach(function (s) {
                if (!current && visible[s.id]) current = s.id;
            });
            if (!current) return;
            links.forEach(function (a) { a.classList.remove('active'); });
            if (byId[current]) byId[current].classList.add('active');
        }, { rootMargin: '-' + (header ? header.offsetHeight : 0) + 'px 0px -60% 0px' });
        document.querySelectorAll('.manual-chapter').forEach(function (s) { io.observe(s); });
    }

    function closeZoom() {
        var box = document.querySelector('.manual-lightbox');
        if (box) box.remove();
    }

    window.manualZoom = function (e, link) {
        e.preventDefault();
        var box = document.createElement('div');
        box.className = 'manual-lightbox';
        var img = document.createElement('img');
        img.src = link.getAttribute('href');
        img.alt = (link.querySelector('img') || {}).alt || '';
        box.appendChild(img);
        box.addEventListener('click', closeZoom);
        document.body.appendChild(box);
    };

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeZoom();
    });
})();
