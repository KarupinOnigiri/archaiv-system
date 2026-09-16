// Scroll-spy: highlights the nav.toc link for whichever section is currently
// in view, so the sidebar always shows where you are in a long document.
(function () {
  var links = Array.prototype.slice.call(document.querySelectorAll('nav.toc a[href^="#"]'));
  if (!links.length) return;

  var linkById = {};
  links.forEach(function (a) {
    linkById[a.getAttribute('href').slice(1)] = a;
  });

  var targets = Object.keys(linkById)
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  if (!targets.length) return;

  // Walk in document order so "the last heading whose top has scrolled past
  // the threshold line" below is always the true current section, top to bottom.
  targets.sort(function (a, b) {
    var pos = a.compareDocumentPosition(b);
    return pos & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  });

  var current = null;
  function setActive(id) {
    if (id === current) return;
    current = id;
    links.forEach(function (a) { a.classList.remove('active'); });
    var link = linkById[id];
    if (link) link.classList.add('active');
  }

  // A heading counts as "current" once it has scrolled up to this line near
  // the top of the viewport - not while merely intersecting some band, so a
  // fast scroll or a jump-link still lands on the right section every time.
  var THRESHOLD = 130;

  var ticking = false;
  function update() {
    ticking = false;

    // Scrolled (near) to the bottom of a short page: the last heading may
    // never reach the threshold line if there isn't enough content below it
    // to push it up that far - in that case it's the current section anyway.
    var atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2;
    if (atBottom) {
      setActive(targets[targets.length - 1].id);
      return;
    }

    var activeId = targets[0].id;
    for (var i = 0; i < targets.length; i++) {
      if (targets[i].getBoundingClientRect().top <= THRESHOLD) {
        activeId = targets[i].id;
      } else {
        break;
      }
    }
    setActive(activeId);
  }

  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(update);
  }

  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  update();
})();
