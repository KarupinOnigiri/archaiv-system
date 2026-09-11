// Global UI chrome shared across every page: theme mode switching, the
// toast notification system, session-timeout modal, notification bell,
// account/profile modals (password, RFID PIN, avatar crop/upload), and
// the give-feedback form. Loaded once by templates/base.html.
//
// Reads per-request/per-user values (current username, toast duration
// preference) from window.APP_CONFIG, which base.html sets via a small
// inline <script> block immediately before this file loads - those
// values are server-rendered per request, so they can't live in this
// static file directly.

// Shared 12-hour/AM-PM formatter for the handful of places that build a
// datetime string via JS template literals instead of Jinja (which has its
// own app.py format_date filter, "%b %d, %Y %I:%M %p" by default - already
// 12-hour, used wherever a template renders one server-side). This is that
// same convention for JS-rendered rows (report tables, member/staff profile
// activity lists, the checkout cart's reservation notice) - every displayed
// clock time in the app should read the same way, never bare 24-hour.
// Accepts the "YYYY-MM-DD HH:MM:SS" (or "YYYY-MM-DDTHH:MM:SS") shape SQLite/
// the API always sends; a date-only "YYYY-MM-DD" (no time component, e.g.
// last_calibrated) passes through unchanged since there's no clock time to
// convert. Falls back to the raw input on anything unparseable.
function formatDateTime12h(raw, opts) {
  if (!raw) return raw;
  const dateOnly = opts && opts.dateOnly;
  const match = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/);
  if (!match) return raw;
  const [, y, mo, d, h, mi] = match;
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const dateStr = `${months[parseInt(mo, 10) - 1]} ${parseInt(d, 10)}, ${y}`;
  if (h === undefined || dateOnly) return dateStr;
  const hourNum = parseInt(h, 10);
  const period = hourNum >= 12 ? 'PM' : 'AM';
  const hour12 = hourNum % 12 === 0 ? 12 : hourNum % 12;
  return `${dateStr} ${hour12}:${mi} ${period}`;
}

(function() {
  const sidebar = document.getElementById('sidebar');
  const toggleBtn = document.getElementById('sidebar-toggle');

  function syncToggleState() {
    const collapsed = sidebar.classList.contains('collapsed');
    toggleBtn.setAttribute('aria-expanded', String(!collapsed));
    const label = (collapsed ? 'Expand sidebar' : 'Collapse sidebar') + ' (Ctrl+E)';
    toggleBtn.setAttribute('aria-label', label);
    toggleBtn.setAttribute('data-tooltip', label);
  }

  function setCollapsed(collapsed) {
    sidebar.classList.toggle('collapsed', collapsed);
    sidebar.setAttribute('aria-expanded', String(!collapsed));
    syncToggleState();
    localStorage.setItem('sidebar-collapsed', collapsed ? '1' : '0');
  }

  if (document.documentElement.classList.contains('sidebar-collapsed-init')) {
    sidebar.classList.add('collapsed');
    document.documentElement.classList.remove('sidebar-collapsed-init');
  }
  sidebar.setAttribute('aria-expanded', String(!sidebar.classList.contains('collapsed')));
  syncToggleState();

  // Asymmetric on purpose: collapsing is a deliberate action (only the
  // dedicated toggle button does it), but expanding blank rail space is
  // easy - while collapsed, clicking empty space (or the toggle button
  // itself) expands it back. Nav links and the account avatar/dropdown
  // are excluded in BOTH states, though, so using a tab or opening the
  // account menu is always just that - collapse/expand stays entirely
  // the user's own choice via the toggle button or blank space, never a
  // side effect of navigating around while collapsed.
  sidebar.addEventListener('click', function(e) {
    if (e.target.closest('a')) return;
    if (e.target.closest('#sidebar-account')) return;
    if (e.target.closest('.dropdown-content')) return;

    if (sidebar.classList.contains('collapsed')) {
      setCollapsed(false);
      return;
    }

    if (e.target.closest('#sidebar-toggle')) {
      setCollapsed(true);
    }
  });

  // Ctrl+E (Cmd+E on Mac) - collapse/expand the sidebar, same toggle as
  // clicking #sidebar-toggle. Chosen over a bare single key (the pattern
  // most pages here use for their own local shortcuts, e.g. Staff's "`")
  // because this one is global - it needs to work from every page
  // without colliding with whatever bare keys that page's own listener
  // already claims. preventDefault() stops it from also hitting whatever
  // the browser itself binds Ctrl/Cmd+E to (varies by browser; usually
  // nothing on the page itself).
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'e' && e.key !== 'E') return;
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
    const target = e.target;
    const isTyping = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
    if (isTyping) return;
    e.preventDefault();
    setCollapsed(!sidebar.classList.contains('collapsed'));
  });

  // Ctrl+/ (Cmd+/ on Mac) - focus the global search bar (content header).
  // NOT Ctrl+K: several browsers/extensions already claim that combo for
  // their own address-bar or command-palette search, which silently ate
  // the keystroke before this page ever saw it. Ctrl+/ is a long-standing,
  // widely-used "focus search" convention (Gmail, Facebook, Trello, ...)
  // that isn't reserved by browser chrome. Global for the same reason
  // Ctrl+E above is: it has to work from every page, not just ones without
  // their own local shortcuts already claiming a bare key. Deliberately NOT
  // blocked while already typing in another field (unlike Ctrl+E) - the
  // whole point is jumping to global search
  // from wherever you are, mid-task in a page's own search box included;
  // a strong, unlikely-to-collide modifier combo like this is exactly the
  // case worth allowing through. Re-focusing an input that's already
  // focused (global search itself) is a harmless no-op either way.
  document.addEventListener('keydown', function (e) {
    if (e.key !== '/' && e.code !== 'Slash') return;
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
    e.preventDefault();
    document.getElementById('global-search-input')?.focus();
  });

  // Alt+Shift+<letter> - jump straight to a sidebar tab. One mnemonic
  // letter per tab (see SIDEBAR_SHORTCUT_KEYS in base.html's nav markup),
  // read off each rendered link's data-shortcut-key rather than a
  // hardcoded route list, so it automatically matches whichever tabs are
  // actually visible for this staff member's role and the current
  // feature flags. Shift is the whole reason this can use mnemonic
  // letters instead of being capped at digits 1-9: plain Alt+<letter> is
  // already claimed per-page all over the app (Alt+N "new checkout",
  // Alt+M "manage categories", Alt+A "add asset"/"mark all read", Alt+R
  // "return items", Alt+G "generate labels", etc.) - adding Shift into
  // the combo means this global shortcut can never collide with any of
  // those local ones, and isn't limited to 9 tabs the way a bare-digit
  // scheme would be.
  document.addEventListener('keydown', function (e) {
    if (!e.altKey || !e.shiftKey || e.ctrlKey || e.metaKey) return;
    if (e.key.length !== 1 || !/[a-zA-Z]/.test(e.key)) return;
    const target = e.target;
    const isTyping = target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable);
    if (isTyping) return;
    if (document.querySelector('dialog[open]')) return;
    const letter = e.key.toUpperCase();
    const link = sidebar.querySelector(`nav .sidebar-link[data-shortcut-key="${letter}"]`);
    if (!link) return;
    e.preventDefault();
    window.location.href = link.getAttribute('href');
  });

  // # # # BOOSTED NAVIGATION -------------------------------------------------
  // base.html's #sidebar-nav carries hx-boost="true" hx-target="#main-content"
  // hx-select="#main-content" - clicking a sidebar link now fetches the page
  // in the background and swaps only #main-content instead of a full browser
  // navigation, so ui.js/htmx/chart.min.js/jspdf.umd.min.js etc. are parsed
  // and executed exactly once per session instead of on every single click.
  //
  // The catch: every page template's own <script> block sits inside
  // #main-content and still does things like
  //   document.addEventListener('keydown', function (e) { ... })
  //   document.body.addEventListener('htmx:configRequest', function (e) { ... })
  //   document.addEventListener('DOMContentLoaded', function () { ... })
  // Under a hard page load those only ever run once. Under boosting, htmx
  // evaluates a swapped-in <script> tag EVERY time #main-content is replaced
  // - so a page visited twice would call document.addEventListener('keydown',
  // ...) twice, and both copies fire forever after (the first one never got
  // torn down; #main-content being destroyed doesn't unregister a listener
  // that was attached to `document`, not to the element itself). Multiply
  // that by every boosted visit and every page gets more duplicate keyboard
  // shortcuts and more duplicate configRequest handlers each time, silently.
  //
  // Fix: pages that need this now assign a single-slot global instead of
  // calling addEventListener directly --
  //   window.pageKeydownHandler = function (e) { ... };
  //   window.pageConfigRequestHandler = function (e) { ... };
  //   window.pageAfterSwapHandler = function (e) { ... };
  // - and ui.js (which lives outside #main-content and is only ever loaded
  // once, boosted or not) owns the ONE real listener of each kind below,
  // forwarding to whichever handler is currently registered. The slot is
  // cleared right before a boosted swap replaces #main-content, so a page
  // that doesn't reassign it on its next visit doesn't leave the previous
  // page's handler running by accident.
  //
  // Only initialize to null if the slot is still untouched (=== undefined),
  // NOT unconditionally - base.html's #block content (where every page's
  // own inline <script> lives, setting these) renders BEFORE this
  // <script src="ui.js"> tag, so on every hard/full page load that page
  // script has already run and set the real handler by the time this line
  // executes. Unconditionally resetting to null here silently discarded
  // that on every hard load - only a later htmx-boosted navigation (which
  // re-runs the swapped-in page script without this file re-executing)
  // ever actually worked, so a plain page load, refresh, or fresh login
  // left every page-local shortcut (this file's own '?' handled
  // separately below, but Escape/'/'/number-key filters/etc., anything
  // registered via pageKeydownHandler) silently dead until the first
  // sidebar click.
  if (typeof window.pageKeydownHandler === 'undefined') window.pageKeydownHandler = null;
  if (typeof window.pageConfigRequestHandler === 'undefined') window.pageConfigRequestHandler = null;
  if (typeof window.pageAfterSwapHandler === 'undefined') window.pageAfterSwapHandler = null;

  document.addEventListener('keydown', function (e) {
    if (typeof window.pageKeydownHandler === 'function') {
      window.pageKeydownHandler(e);
    }
  });

  document.body.addEventListener('htmx:configRequest', function (e) {
    if (typeof window.pageConfigRequestHandler === 'function') {
      window.pageConfigRequestHandler(e);
    }
  });

  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (typeof window.pageAfterSwapHandler === 'function') {
      window.pageAfterSwapHandler(e);
    }
  });

  document.body.addEventListener('htmx:beforeSwap', function (e) {
    if (e.detail && e.detail.target && e.detail.target.id === 'main-content') {
      window.pageKeydownHandler = null;
      window.pageConfigRequestHandler = null;
      window.pageAfterSwapHandler = null;
    }
  });

  // One shared #app-tooltip bubble for every [data-tooltip] element, delegated on document
  // (mouseover bubbles, so this still covers stuff htmx swaps in later). Rail tooltips
  // (sidebar links, account avatar) only show while collapsed unless data-tooltip-show-expanded
  // is set too (sidebar tabs use this to surface their Alt+Shift+letter shortcut even expanded).
  // Everything else just shows on hover, positioned below-center. 150ms show-delay, instant hide.
  const TOOLTIP_SHOW_DELAY_MS = 150;
  const tooltipEl = document.getElementById('app-tooltip');
  let tooltipShowTimer = null;
  let tooltipTarget = null;

  function hideTooltip() {
    if (tooltipShowTimer) {
      clearTimeout(tooltipShowTimer);
      tooltipShowTimer = null;
    }
    tooltipEl.classList.add('hidden');
    tooltipTarget = null;
  }

  document.addEventListener('mouseover', function (e) {
    const el = e.target.closest('[data-tooltip]');
    if (!el || el === tooltipTarget) return;
    const isRail = el.hasAttribute('data-tooltip-rail');
    const showExpanded = el.hasAttribute('data-tooltip-show-expanded');
    if (isRail && !showExpanded && !sidebar.classList.contains('collapsed')) return;

    tooltipTarget = el;
    if (tooltipShowTimer) clearTimeout(tooltipShowTimer);
    tooltipShowTimer = setTimeout(function () {
      const text = el.getAttribute('data-tooltip');
      if (!text) return;
      tooltipEl.textContent = text;
      tooltipEl.classList.remove('hidden');
      // Measured AFTER unhiding (position doesn't affect a fixed,
      // content-sized bubble's own width/height, so this is safe to read
      // before top/left are set below) - real per-tooltip size instead of
      // one guessed constant, needed now that placement is picked in both
      // axes instead of just flipping vertically near the bottom edge.
      const tw = tooltipEl.offsetWidth;
      const th = tooltipEl.offsetHeight;
      const rect = el.getBoundingClientRect();
      const GAP = 8;
      const MARGIN = 8; // min breathing room against the viewport edge

      if (isRail) {
        // Prefer right-of-element (the icon column's own free space) -
        // flip to the left instead whenever the rail itself is hugging
        // the right edge and there isn't room to grow rightward (e.g. an
        // icon in a right-docked panel, if one is ever added - collapsed
        // sidebar tabs today always have room to their right already).
        const roomRight = window.innerWidth - rect.right - GAP;
        const left = roomRight >= tw + MARGIN ? rect.right + GAP : rect.left - GAP - tw;
        tooltipEl.style.top = (rect.top + rect.height / 2 - th / 2) + 'px';
        tooltipEl.style.left = left + 'px';
        tooltipEl.style.transform = '';
        return;
      }

      // Everything else: below-center by default, flipped/clamped on
      // whichever axis would otherwise push the bubble off-screen - not
      // just the bottom edge (the account row's name span, sitting right
      // at the sidebar's bottom edge, was the one case this used to
      // special-case into the rail's right-side placement instead; it
      // now naturally flips to "above" here like anything else hugging
      // the bottom would). A button near the right edge or a
      // bottom/top-right corner no longer grows off-screen either - the
      // horizontal center clamps inward, and if that's not enough (the
      // element itself is right at the edge) it right-aligns against the
      // viewport instead of the element's own center.
      const showAbove = rect.bottom + GAP + th > window.innerHeight - MARGIN
        && rect.top - GAP - th >= MARGIN;
      const top = showAbove ? (rect.top - GAP - th) : (rect.bottom + GAP);

      let left = rect.left + rect.width / 2 - tw / 2;
      if (left + tw + MARGIN > window.innerWidth) left = window.innerWidth - tw - MARGIN;
      if (left < MARGIN) left = MARGIN;

      tooltipEl.style.top = top + 'px';
      tooltipEl.style.left = left + 'px';
      tooltipEl.style.transform = '';
    }, TOOLTIP_SHOW_DELAY_MS);
  });

  document.addEventListener('mouseout', function (e) {
    const el = e.target.closest('[data-tooltip]');
    if (!el || el.contains(e.relatedTarget)) return;
    hideTooltip();
  });

  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-tooltip]')) hideTooltip();
  });

  // Collapsing mid-hover (via the toggle button, e.g.) shouldn't leave a
  // stale tooltip positioned over now-hidden content; expanding should
  // never show one at all (see the isRail guard above, but a tooltip
  // already showing when the button is clicked to expand needs to be
  // explicitly cleared too).
  toggleBtn.addEventListener('click', hideTooltip);
})();

// Full Help modal (#full-help-modal) - TOC click + scroll-spy. Lives
// inside a <dialog>, not the page itself, so a plain <a href="#id"> can't
// be left to the browser's native anchor-jump: that scrolls (and
// URL-hashes) the outer window, not #full-help-scroll, which is the
// element that's actually scrollable here. Every TOC link intercepts its
// click and scrolls the content pane directly instead.
//
// The active-link highlight (which section you're "on" while scrolling)
// is driven by a plain 'scroll' listener + getBoundingClientRect() checks
// on every section, NOT IntersectionObserver - tried that first, and
// confirmed (via a bare-minimum viewport-rooted observer, no custom root
// or rootMargin involved) that it never fires at all for anything inside
// a native <dialog> shown via showModal(): elements promoted to the
// browser's top layer apparently don't participate in intersection
// tracking the normal way. rAF-throttled so this can't run more than
// once per frame even during a fast scroll.
(function () {
  const modal = document.getElementById('full-help-modal');
  if (!modal) return;
  const scrollPane = document.getElementById('full-help-scroll');
  const tocLinks = Array.from(modal.querySelectorAll('[data-help-toc-link]'));
  if (!scrollPane || tocLinks.length === 0) return;

  const sections = tocLinks
    .map(function (link) { return document.getElementById(link.getAttribute('href').slice(1)); })
    .filter(Boolean);

  function setActiveLink(id) {
    tocLinks.forEach(function (link) {
      const active = link.getAttribute('href') === '#' + id;
      link.classList.toggle('bg-base-200', active);
      link.classList.toggle('text-base-content', active);
      link.classList.toggle('font-semibold', active);
      link.classList.toggle('text-base-content/70', !active);
    });
  }

  tocLinks.forEach(function (link) {
    link.addEventListener('click', function (e) {
      e.preventDefault();
      const id = link.getAttribute('href').slice(1);
      const target = document.getElementById(id);
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setActiveLink(id);
    });
  });

  // Trigger line sits a little below the pane's own top edge (roughly
  // where a heading ends up right after scrolling to it), not dead
  // center - the last section whose heading has crossed above that line
  // is "current." Sections are in document order, so the first one that
  // *hasn't* crossed it yet means every later one hasn't either; no need
  // to keep checking past that point.
  const TRIGGER_OFFSET_PX = 96;

  function updateActiveFromScroll() {
    // Scrolled all the way to the bottom - the last section forces
    // itself active here rather than via the trigger-line loop below,
    // since a short final section (not much page left beneath it) may
    // never actually scroll far enough to cross that line on its own,
    // which would otherwise leave the second-to-last section looking
    // "current" even once there's nothing left to scroll to.
    const atBottom = scrollPane.scrollTop + scrollPane.clientHeight >= scrollPane.scrollHeight - 1;
    if (atBottom) {
      setActiveLink(sections[sections.length - 1].id);
      return;
    }

    const paneTop = scrollPane.getBoundingClientRect().top;
    const triggerY = paneTop + TRIGGER_OFFSET_PX;
    let current = sections[0];
    for (let i = 0; i < sections.length; i++) {
      if (sections[i].getBoundingClientRect().top <= triggerY) {
        current = sections[i];
      } else {
        break;
      }
    }
    if (current) setActiveLink(current.id);
  }

  // Called directly on every 'scroll' event, no rAF/debounce wrapper --
  // there are only a handful of sections (~13 at most, one per app tab),
  // so the getBoundingClientRect() loop above is cheap enough not to need
  // throttling, and skipping it sidesteps rAF's own quirk of not firing
  // at all on a backgrounded/non-compositing tab.
  scrollPane.addEventListener('scroll', updateActiveFromScroll);

  // showModal() doesn't fire a load/open event browsers expose directly,
  // so this hooks the button that actually calls showModal() on this
  // dialog (the "Open full Help" row) - simplest reliable "this just
  // became visible" signal available. Resets scroll position + highlight
  // to the top on every open, same as a fresh page load would.
  document.querySelectorAll('[onclick*="full-help-modal\').showModal"]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      scrollPane.scrollTop = 0;
      setActiveLink(sections[0] ? sections[0].id : tocLinks[0].getAttribute('href').slice(1));
    });
  });
})();

// A disabled-tab redirect (services/auth_service.py's feature_required)
// lands here as '/?feature_disabled=<label>' - this app has no
// server-side flash(), so the one-shot message rides a query param
// instead and gets toasted client-side. Wrapped in DOMContentLoaded (even
// though this script tag already runs after the DOM is parsed) so it
// runs after showToast's own <script> block further down the page has
// executed, regardless of where in the document this ends up.
document.addEventListener('DOMContentLoaded', function () {
  const params = new URLSearchParams(window.location.search);
  const disabledFeature = params.get('feature_disabled');
  if (disabledFeature) {
    showToast(`${disabledFeature} is currently disabled`, 'warning');
    params.delete('feature_disabled');
    const newSearch = params.toString();
    const newUrl = window.location.pathname + (newSearch ? `?${newSearch}` : '') + window.location.hash;
    history.replaceState(null, '', newUrl);
  }

  // Same one-shot-param pattern as feature_disabled above - an old
  // bookmark/link to the /help page (routes/pages.py's help_page, now a
  // redirect) lands here as '/?open_help=1'; this is what actually opens
  // the modal that page used to be, then cleans the param back off the
  // URL so a refresh doesn't reopen it.
  const openHelp = params.get('open_help');
  if (openHelp) {
    // .click() (not a direct .showModal() call) so this goes through the
    // exact same button the "Open full Help" row uses - that button has
    // its own click listener (see the scroll-spy IIFE above) that resets
    // the TOC highlight/scroll position and starts the IntersectionObserver;
    // calling showModal() directly here would open the dialog without any
    // of that ever running.
    const openHelpBtn = document.querySelector('[onclick*="full-help-modal\').showModal"]');
    if (openHelpBtn) openHelpBtn.click();
    params.delete('open_help');
    const newSearch = params.toString();
    const newUrl = window.location.pathname + (newSearch ? `?${newSearch}` : '') + window.location.hash;
    history.replaceState(null, '', newUrl);
  }
});

// Accumulated across possibly-repeated "Add screenshot" clicks - a plain
// <input multiple> alone would replace its file list on every pick, so
// this array (and the previews rendered from it) is the actual source of
// truth for what gets uploaded, not the <input>'s own .files.
const MAX_FEEDBACK_SCREENSHOTS = 5;
let feedbackScreenshotFiles = [];

function resetGiveFeedbackForm() {
  const form = document.getElementById('give-feedback-form');
  if (form) form.reset();
  const errorEl = document.getElementById('give-feedback-error');
  if (errorEl) errorEl.classList.add('invisible');
  const btn = document.getElementById('give-feedback-submit-btn');
  if (btn) { btn.disabled = false; btn.textContent = 'Send feedback'; }
  feedbackScreenshotFiles = [];
  renderFeedbackScreenshotPreviews();
}

function renderFeedbackScreenshotPreviews() {
  const container = document.getElementById('feedback-screenshot-previews');
  container.innerHTML = '';
  feedbackScreenshotFiles.forEach((file, index) => {
    const url = URL.createObjectURL(file);
    const wrap = document.createElement('div');
    wrap.className = 'relative w-14 h-14 rounded-lg overflow-hidden border border-base-300 shrink-0';
    wrap.innerHTML = `
      <img src="${url}" alt="" class="w-full h-full object-cover">
      <button type="button" aria-label="Remove screenshot"
              class="absolute top-0 right-0 bg-black/60 hover:bg-black/80 text-white w-4 h-4 flex items-center justify-center text-xs leading-none">&times;</button>
    `;
    wrap.querySelector('button').onclick = () => {
      feedbackScreenshotFiles.splice(index, 1);
      renderFeedbackScreenshotPreviews();
    };
    container.appendChild(wrap);
  });

  document.getElementById('feedback-screenshot-count').textContent =
    `${feedbackScreenshotFiles.length}/${MAX_FEEDBACK_SCREENSHOTS}`;
  document.getElementById('feedback-screenshot-add-btn').disabled =
    feedbackScreenshotFiles.length >= MAX_FEEDBACK_SCREENSHOTS;
}

function handleFeedbackScreenshotSelect(event) {
  const picked = Array.from(event.target.files || []);
  event.target.value = ''; // so picking the same file again still fires 'change'

  const room = MAX_FEEDBACK_SCREENSHOTS - feedbackScreenshotFiles.length;
  if (room <= 0) return;

  const errorEl = document.getElementById('give-feedback-error');
  if (picked.length > room) {
    errorEl.textContent = `Only ${room} more screenshot${room === 1 ? '' : 's'} can be added (max ${MAX_FEEDBACK_SCREENSHOTS}).`;
    errorEl.classList.remove('invisible');
  }
  feedbackScreenshotFiles.push(...picked.slice(0, room));
  renderFeedbackScreenshotPreviews();
}

async function submitGiveFeedback(event) {
  event.preventDefault();

  const errorEl = document.getElementById('give-feedback-error');
  errorEl.classList.add('invisible');

  const category = document.querySelector('input[name="feedback-category"]:checked')?.value;
  const message = document.getElementById('feedback-message').value.trim();

  if (!message) {
    errorEl.textContent = 'Say a bit about what happened or what you have in mind.';
    errorEl.classList.remove('invisible');
    return false;
  }

  const btn = document.getElementById('give-feedback-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Sending...';

  const formData = new FormData();
  formData.append('category', category);
  formData.append('message', message);
  formData.append('page_path', window.location.pathname);
  feedbackScreenshotFiles.forEach((file) => formData.append('screenshots', file));

  try {
    const res = await fetch('/api/v1/feedback', {
      method: 'POST',
      headers: {
        // No Content-Type here - the browser sets multipart/form-data
        // with the correct boundary itself; setting it manually on a
        // FormData body breaks the boundary and the server can't parse it.
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: formData,
    });

    if (res.status === 401) {
      showSessionTimeoutModal();
      return false;
    }

    const data = await res.json();
    if (!res.ok) {
      errorEl.textContent = data.error || "Couldn't send feedback.";
      errorEl.classList.remove('invisible');
      btn.disabled = false;
      btn.textContent = 'Send feedback';
      return false;
    }

    document.getElementById('give-feedback-modal').close();
    showToast(data.message || 'Thanks - feedback sent', 'success');
  } catch (e) {
    errorEl.textContent = "Couldn't send feedback. Check your connection and try again.";
    errorEl.classList.remove('invisible');
    btn.disabled = false;
    btn.textContent = 'Send feedback';
  }
  return false;
}

// Generalizes login.html's togglePassword() to any field id - expects a
// sibling button #<fieldId>-toggle-btn and two SVGs #<fieldId>-eye-open/
// -eye-closed, all emitted together by base.html's password_toggle_field
// macro. Only the `type` attribute flips (password <-> text); nothing
// else about the field (inputmode/pattern for the PIN fields) is touched.
function toggleFieldVisibility(fieldId) {
  const field = document.getElementById(fieldId);
  const eyeOpen = document.getElementById(fieldId + '-eye-open');
  const eyeClosed = document.getElementById(fieldId + '-eye-closed');
  if (!field) return;
  if (field.type === 'password') {
    field.type = 'text';
    eyeOpen.classList.remove('hidden');
    eyeClosed.classList.add('hidden');
  } else {
    field.type = 'password';
    eyeOpen.classList.add('hidden');
    eyeClosed.classList.remove('hidden');
  }
}

// Same "only show the toggle once there's something to reveal" behavior
// as login.html's password field - wired once per field id here rather
// than repeated inline per input.
function wirePasswordToggleField(fieldId) {
  const field = document.getElementById(fieldId);
  const btn = document.getElementById(fieldId + '-toggle-btn');
  if (!field || !btn) return;
  field.addEventListener('input', function () {
    btn.classList.toggle('hidden', this.value.length === 0);
  });
}

// cp-current/cp-new/cp-confirm (Change Password) no longer render a toggle
// button at all - see base.html's password_toggle_field show_toggle param.
['crp-current-password', 'crp-new-pin', 'crp-confirm-pin']
  .forEach(wirePasswordToggleField);

// Restores every password_toggle_field back to its default hidden-text,
// toggle-hidden state - form.reset() clears values but not a `type`
// attribute changed imperatively by toggleFieldVisibility, so a field
// left toggled to "text" would otherwise still show as "text" the next
// time this modal opens even after being reset/closed.
function resetPasswordToggleField(fieldId) {
  const field = document.getElementById(fieldId);
  const btn = document.getElementById(fieldId + '-toggle-btn');
  if (field) field.type = 'password';
  if (btn) btn.classList.add('hidden');
}

function resetChangePasswordForm() {
  const form = document.getElementById('change-password-form');
  if (form) form.reset();
  const errorEl = document.getElementById('change-password-error');
  if (errorEl) errorEl.classList.add('invisible');
  const btn = document.getElementById('change-password-submit-btn');
  if (btn) { btn.disabled = false; btn.textContent = 'Update password'; }
  ['cp-current', 'cp-new', 'cp-confirm'].forEach(resetPasswordToggleField);
}

async function submitChangePassword(event) {
  event.preventDefault();

  const errorEl = document.getElementById('change-password-error');
  errorEl.classList.add('invisible');

  const currentPassword = document.getElementById('cp-current').value;
  const newPassword = document.getElementById('cp-new').value;
  const confirmPassword = document.getElementById('cp-confirm').value;

  if (!currentPassword || !newPassword || !confirmPassword) {
    errorEl.textContent = 'Fill in all three fields.';
    errorEl.classList.remove('invisible');
    return false;
  }
  if (newPassword !== confirmPassword) {
    errorEl.textContent = "New password and confirmation don't match.";
    errorEl.classList.remove('invisible');
    return false;
  }
  if (newPassword.length < 8) {
    errorEl.textContent = 'New password must be at least 8 characters.';
    errorEl.classList.remove('invisible');
    return false;
  }

  const btn = document.getElementById('change-password-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Updating...';

  try {
    const res = await fetch('/api/v1/staff/change-password', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    });

    const data = await res.json().catch(() => ({}));

    // change_password_submit returns 401 for two different reasons: a
    // wrong current password (a normal, expected form error) or an
    // expired/invalid session (via require_session, same as every other
    // route). Distinguish by message so a mistyped current password
    // doesn't incorrectly pop the "session timed out" modal.
    if (res.status === 401 && data.error !== 'Current password is incorrect') {
      showSessionTimeoutModal();
      return false;
    }

    if (!res.ok) {
      errorEl.textContent = data.error || "Couldn't change password. Please try again.";
      errorEl.classList.remove('invisible');
      btn.disabled = false;
      btn.textContent = 'Update password';
      return false;
    }

    document.getElementById('change-password-modal').close();
    showToast('Password changed successfully', 'success');
  } catch (err) {
    errorEl.textContent = 'Network error - check your connection and try again.';
    errorEl.classList.remove('invisible');
    btn.disabled = false;
    btn.textContent = 'Update password';
  }

  return false;
}

// The RFID-PIN-specific resetChangeRfidPinForm()/submitChangeRfidPin() that
// used to sit here (posting to /api/v1/staff/change-rfid-pin) were deleted
// along with that whole login route and its modal - see templates/base.html's
// comment above the Barcode + PIN row. openBarcodePinModal/submitBarcodePinForm
// below cover PIN-setting now.

// Barcode + PIN login (self-service, every role) - one shared modal for
// both first-time enable and a later change, since both endpoints do the
// same write (set rfid_pin_hash) and only differ in whether
// barcode_pin_enabled also flips on. openBarcodePinModal(mode) picks which;
// submitBarcodePinForm() reads that mode back to choose the endpoint and
// button/title text. See routes/staff.py's enable/change-barcode-pin routes.
let barcodePinModalMode = 'enable';

function openBarcodePinModal(mode) {
  barcodePinModalMode = mode;
  document.getElementById('barcode-pin-modal-title').textContent =
    mode === 'change' ? 'Change barcode + PIN' : 'Enable barcode + PIN login';
  document.getElementById('barcode-pin-submit-btn').textContent =
    mode === 'change' ? 'Update PIN' : 'Enable';
  document.getElementById('barcode-pin-modal').showModal();
}

function resetBarcodePinForm() {
  const form = document.getElementById('barcode-pin-form');
  if (form) form.reset();
  const errorEl = document.getElementById('barcode-pin-error');
  if (errorEl) errorEl.classList.add('invisible');
  const btn = document.getElementById('barcode-pin-submit-btn');
  if (btn) { btn.disabled = false; btn.textContent = barcodePinModalMode === 'change' ? 'Update PIN' : 'Enable'; }
  ['bp-current-password', 'bp-new-pin', 'bp-confirm-pin'].forEach(resetPasswordToggleField);
}

async function submitBarcodePinForm(event) {
  event.preventDefault();

  const errorEl = document.getElementById('barcode-pin-error');
  errorEl.classList.add('invisible');

  const currentPassword = document.getElementById('bp-current-password').value;
  const newPin = document.getElementById('bp-new-pin').value;
  const confirmPin = document.getElementById('bp-confirm-pin').value;

  if (!currentPassword || !newPin || !confirmPin) {
    errorEl.textContent = 'Fill in all three fields.';
    errorEl.classList.remove('invisible');
    return false;
  }
  if (newPin !== confirmPin) {
    errorEl.textContent = "New PIN and confirmation don't match.";
    errorEl.classList.remove('invisible');
    return false;
  }
  if (!/^\d{6}$/.test(newPin)) {
    errorEl.textContent = 'New PIN must be exactly 6 digits.';
    errorEl.classList.remove('invisible');
    return false;
  }

  const btn = document.getElementById('barcode-pin-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Updating...';

  const endpoint = barcodePinModalMode === 'change'
    ? '/api/v1/staff/change-barcode-pin'
    : '/api/v1/staff/barcode-pin/enable';

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({ current_password: currentPassword, new_pin: newPin }),
    });

    const data = await res.json().catch(() => ({}));

    // Same reasoning as submitChangePassword's identical check - 401 here
    // can mean "wrong current password" (expected form error) or "your
    // session expired" (require_session), and only the second should pop
    // the session-timeout modal.
    if (res.status === 401 && data.error !== 'Current password is incorrect') {
      showSessionTimeoutModal();
      return false;
    }

    if (!res.ok) {
      errorEl.textContent = data.error || "Couldn't save. Please try again.";
      errorEl.classList.remove('invisible');
      btn.disabled = false;
      btn.textContent = barcodePinModalMode === 'change' ? 'Update PIN' : 'Enable';
      return false;
    }

    document.getElementById('barcode-pin-modal').close();
    showToast(data.message || 'Saved successfully', 'success');
    // Reloads so the account modal's row (Enable vs Change PIN/Disable)
    // reflects the new barcode_pin_enabled state on next open.
    setTimeout(function () { window.location.reload(); }, 400);
  } catch (err) {
    errorEl.textContent = 'Network error - check your connection and try again.';
    errorEl.classList.remove('invisible');
    btn.disabled = false;
    btn.textContent = barcodePinModalMode === 'change' ? 'Update PIN' : 'Enable';
  }

  return false;
}

function resetDisableBarcodePinForm() {
  const form = document.getElementById('disable-barcode-pin-form');
  if (form) form.reset();
  const errorEl = document.getElementById('disable-barcode-pin-error');
  if (errorEl) errorEl.classList.add('invisible');
  const btn = document.getElementById('disable-barcode-pin-submit-btn');
  if (btn) { btn.disabled = false; btn.textContent = 'Disable'; }
  resetPasswordToggleField('dbp-current-password');
}

async function submitDisableBarcodePin(event) {
  event.preventDefault();

  const errorEl = document.getElementById('disable-barcode-pin-error');
  errorEl.classList.add('invisible');

  const currentPassword = document.getElementById('dbp-current-password').value;
  if (!currentPassword) {
    errorEl.textContent = 'Enter your current password.';
    errorEl.classList.remove('invisible');
    return false;
  }

  const btn = document.getElementById('disable-barcode-pin-submit-btn');
  btn.disabled = true;
  btn.textContent = 'Disabling...';

  try {
    const res = await fetch('/api/v1/staff/barcode-pin/disable', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify({ current_password: currentPassword }),
    });

    const data = await res.json().catch(() => ({}));

    if (res.status === 401 && data.error !== 'Current password is incorrect') {
      showSessionTimeoutModal();
      return false;
    }

    if (!res.ok) {
      errorEl.textContent = data.error || "Couldn't disable. Please try again.";
      errorEl.classList.remove('invisible');
      btn.disabled = false;
      btn.textContent = 'Disable';
      return false;
    }

    document.getElementById('disable-barcode-pin-modal').close();
    showToast(data.message || 'Barcode + PIN login disabled', 'success');
    setTimeout(function () { window.location.reload(); }, 400);
  } catch (err) {
    errorEl.textContent = 'Network error - check your connection and try again.';
    errorEl.classList.remove('invisible');
    btn.disabled = false;
    btn.textContent = 'Disable';
  }

  return false;
}

const PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024; // 5MB
const PROFILE_PHOTO_ALLOWED_TYPES = ['image/png', 'image/jpeg', 'image/webp'];

// Swaps every avatar spot (sidebar rail, dropdown card, account modal) to
// the same <img>, or back to initials when url is falsy. Three separate
// containers exist because each one is a different size/context, but they
// always show the same photo, so a successful upload (or a removal) has to
// touch all three at once rather than just the one the user clicked.
function renderAvatars(url) {
  const targets = ['sidebar-avatar', 'dropdown-avatar', 'account-modal-avatar'];
  targets.forEach(function (id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = url
      ? `<img src="${url}" alt="" class="w-full h-full object-cover">`
      : `<span class="font-semibold">${(window.APP_CONFIG.staffUsername[0] || '').toUpperCase()}</span>`;
  });

  // account-modal-avatar is the only one that's clickable (opens the
  // lightbox) - its cursor/hover styling is set server-side to match
  // whatever staff.photo_path was at page load, but an upload changes
  // that client-side without a reload, so it has to be resynced here too.
  const accountAvatar = document.getElementById('account-modal-avatar');
  if (accountAvatar) {
    accountAvatar.classList.toggle('cursor-pointer', !!url);
    accountAvatar.classList.toggle('hover:opacity-90', !!url);
    accountAvatar.classList.toggle('cursor-default', !url);
    accountAvatar.title = url ? 'View photo - drop or paste an image to replace it' : 'Drop or paste an image to add one';
  }

  const removeBtn = document.getElementById('profile-photo-remove-btn');
  if (removeBtn) removeBtn.classList.toggle('hidden', !url);
}

// Opens any image at full size in the one shared lightbox (defined once in
// base.html, so it works from every page without each needing its own copy).
// `src`/`alt` cover every OTHER caller (asset photos, staff photos, etc.) -
// passed straight through to the lightbox <img>. Called with no arguments
// at all, it falls back to the My Account avatar's own <img> (its original,
// still-supported use before this became general-purpose): no-ops when
// that avatar is still showing initials (nothing to enlarge), reading the
// live DOM rather than a server-rendered flag so it stays correct right
// after an upload, before any page reload.
function openPhotoLightbox(src, alt) {
  let url = src;
  if (!url) {
    const avatarImg = document.querySelector('#account-modal-avatar img');
    if (!avatarImg) return;
    url = avatarImg.src;
  }
  const img = document.getElementById('photo-lightbox-img');
  img.src = url;
  img.alt = alt || 'Photo';
  document.getElementById('photo-lightbox-modal').showModal();
}

function copyUsername() {
  const username = window.APP_CONFIG.staffUsername;
  navigator.clipboard.writeText(username)
    .then(() => showToast('Username copied', 'success'))
    .catch(() => showToast("Couldn't copy - select and copy manually", 'error'));
}

function validateProfilePhotoFile(file) {
  if (!file) return 'No file selected.';
  if (!PROFILE_PHOTO_ALLOWED_TYPES.includes(file.type)) {
    return 'Please choose a PNG, JPEG, or WebP image.';
  }
  if (file.size > PROFILE_PHOTO_MAX_BYTES) {
    return 'That image is too large - please choose one under 5MB.';
  }
  return null;
}

function handleProfilePhotoSelect(event) {
  const input = event.target;
  const file = input.files && input.files[0];
  openPickedPhoto(file, input);
}

// Shared entry point for every way a photo can be picked (file input,
// drag-and-drop onto the avatar, or pasting an image) - validates once,
// then opens the crop modal rather than uploading straight away.
function openPickedPhoto(file, input) {
  const error = validateProfilePhotoFile(file);
  if (error) {
    showToast(error, 'error');
    if (input) input.value = '';
    return;
  }
  openCropModal(file, input);
}

function handleAvatarDrop(event) {
  event.preventDefault();
  event.stopPropagation();
  event.currentTarget.classList.remove('ring-2', 'ring-primary');
  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
  openPickedPhoto(file, null);
}

// Pasting an image (Ctrl+V) while the account modal is open replaces the
// photo the same way a drop would. Scoped to the whole document since the
// browser puts focus all over the modal (buttons, nothing at all), not
// reliably on the avatar itself - gated on the modal being open so paste
// elsewhere in the app is untouched.
document.addEventListener('paste', function (event) {
  const modal = document.getElementById('my-account-modal');
  if (!modal || !modal.open) return;
  const items = event.clipboardData && event.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) openPickedPhoto(file, null);
      event.preventDefault();
      return;
    }
  }
});

// # # # PHOTO CROP MODAL --------------------------------------------------
// Circular-viewport pan-and-zoom cropper. cropState tracks everything
// needed to redraw the preview and, on Save, to bake the same transform
// onto an offscreen canvas at a fixed output size - so what's previewed
// behind the circular mask is exactly what gets uploaded.
const CROP_VIEWPORT = 260; // px, must match #crop-viewport's w/h in the template
const CROP_OUTPUT = 480; // px, exported square image size
let cropState = null;

function openCropModal(file, sourceInput) {
  const img = document.getElementById('crop-image');
  const objectUrl = URL.createObjectURL(file);

  cropState = {
    file,
    sourceInput,
    objectUrl,
    naturalW: 0,
    naturalH: 0,
    baseScale: 1,
    zoom: 1, // 1x-3x, on top of baseScale
    panX: 0,
    panY: 0,
    dragging: false,
    startX: 0,
    startY: 0,
    startPanX: 0,
    startPanY: 0,
  };

  img.onload = function () {
    cropState.naturalW = img.naturalWidth;
    cropState.naturalH = img.naturalHeight;
    // baseScale covers the circular viewport with no gaps at zoom = 1x,
    // same idea as CSS object-fit: cover.
    cropState.baseScale = Math.max(CROP_VIEWPORT / img.naturalWidth, CROP_VIEWPORT / img.naturalHeight);
    cropState.panX = (CROP_VIEWPORT - img.naturalWidth * cropState.baseScale) / 2;
    cropState.panY = (CROP_VIEWPORT - img.naturalHeight * cropState.baseScale) / 2;
    updateCropZoom(100); // also syncs the zoom-out button's disabled state
  };
  img.src = objectUrl;

  document.getElementById('photo-crop-modal').showModal();
}

function closeCropModal() {
  if (cropState) {
    URL.revokeObjectURL(cropState.objectUrl);
    if (cropState.sourceInput) cropState.sourceInput.value = '';
    cropState = null;
  }
}

function currentCropScale() {
  return cropState.baseScale * cropState.zoom;
}

// Keeps the image covering the viewport at all times - panX/panY are
// clamped so the viewport's circular mask never shows empty space.
function clampCropPan() {
  const scale = currentCropScale();
  const dispW = cropState.naturalW * scale;
  const dispH = cropState.naturalH * scale;
  const minX = Math.min(0, CROP_VIEWPORT - dispW);
  const minY = Math.min(0, CROP_VIEWPORT - dispH);
  cropState.panX = Math.max(minX, Math.min(0, cropState.panX));
  cropState.panY = Math.max(minY, Math.min(0, cropState.panY));
}

function applyCropTransform() {
  clampCropPan();
  const scale = currentCropScale();
  const img = document.getElementById('crop-image');
  img.style.width = `${cropState.naturalW * scale}px`;
  img.style.height = `${cropState.naturalH * scale}px`;
  img.style.transform = `translate(${cropState.panX}px, ${cropState.panY}px)`;
}

function updateCropZoom(sliderValue) {
  if (!cropState) return;
  const clamped = Math.max(100, Math.min(300, sliderValue));
  cropState.zoom = clamped / 100;
  document.getElementById('crop-zoom').value = clamped;
  document.getElementById('crop-zoom-out-btn').disabled = clamped <= 100;
  document.getElementById('crop-zoom-in-btn').disabled = clamped >= 300;
  applyCropTransform();
}

// Click handlers for the magnifier icons flanking the slider - same
// clamped 100-300 range, just nudged by `delta` instead of dragged. Below
// 100 the photo would no longer fully cover the circular mask (gaps at
// the edges), so 100 is a hard floor, not just the slider's default.
function stepCropZoom(delta) {
  if (!cropState) return;
  const slider = document.getElementById('crop-zoom');
  updateCropZoom(Number(slider.value) + delta);
}

(function initCropDragAndWheel() {
  const viewport = document.getElementById('crop-viewport');
  if (!viewport) return;

  function pointerDown(clientX, clientY) {
    if (!cropState) return;
    cropState.dragging = true;
    cropState.startX = clientX;
    cropState.startY = clientY;
    cropState.startPanX = cropState.panX;
    cropState.startPanY = cropState.panY;
    viewport.classList.add('cursor-grabbing');
    viewport.classList.remove('cursor-grab');
  }
  function pointerMove(clientX, clientY) {
    if (!cropState || !cropState.dragging) return;
    cropState.panX = cropState.startPanX + (clientX - cropState.startX);
    cropState.panY = cropState.startPanY + (clientY - cropState.startY);
    applyCropTransform();
  }
  function pointerUp() {
    if (!cropState) return;
    cropState.dragging = false;
    viewport.classList.remove('cursor-grabbing');
    viewport.classList.add('cursor-grab');
  }

  viewport.addEventListener('pointerdown', (e) => { viewport.setPointerCapture(e.pointerId); pointerDown(e.clientX, e.clientY); });
  viewport.addEventListener('pointermove', (e) => pointerMove(e.clientX, e.clientY));
  viewport.addEventListener('pointerup', pointerUp);
  viewport.addEventListener('pointercancel', pointerUp);

  // Scroll wheel zooms, same range as the slider, so trackpad/mouse users
  // don't have to reach for it.
  viewport.addEventListener('wheel', (e) => {
    if (!cropState) return;
    e.preventDefault();
    stepCropZoom(-e.deltaY * 0.2);
  }, { passive: false });
})();

async function saveCroppedPhoto() {
  if (!cropState) return;
  const saveBtn = document.getElementById('crop-save-btn');
  const saveSpinner = document.getElementById('crop-save-spinner');
  saveBtn.disabled = true;
  saveSpinner.classList.remove('hidden');

  try {
    const img = document.getElementById('crop-image');
    const outScale = CROP_OUTPUT / CROP_VIEWPORT;
    const scale = currentCropScale() * outScale;

    const canvas = document.createElement('canvas');
    canvas.width = CROP_OUTPUT;
    canvas.height = CROP_OUTPUT;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(
      img,
      cropState.panX * outScale,
      cropState.panY * outScale,
      cropState.naturalW * scale,
      cropState.naturalH * scale
    );

    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
    if (!blob) {
      showToast("Couldn't process that image. Please try again.", 'error');
      return;
    }

    const sourceInput = cropState.sourceInput;
    document.getElementById('photo-crop-modal').close();
    await uploadProfilePhoto(blob, sourceInput);
  } finally {
    saveBtn.disabled = false;
    saveSpinner.classList.add('hidden');
  }
}

async function uploadProfilePhoto(blob, input) {
  const btn = document.getElementById('profile-photo-btn');
  const spinner = document.getElementById('account-modal-avatar-spinner');
  btn.disabled = true;
  spinner.classList.remove('hidden');

  const formData = new FormData();
  formData.append('photo', blob, 'profile.jpg');

  try {
    const res = await fetch('/api/v1/staff/photo', {
      method: 'POST',
      headers: {
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
      body: formData,
    });

    const data = await res.json().catch(() => ({}));

    if (res.status === 401) {
      showSessionTimeoutModal();
      return;
    }

    if (!res.ok) {
      showToast(data.error || "Couldn't upload photo. Please try again.", 'error');
      return;
    }

    renderAvatars(data.photo_url);
    showToast('Profile photo updated', 'success');
  } catch (err) {
    showToast('Network error - check your connection and try again.', 'error');
  } finally {
    btn.disabled = false;
    spinner.classList.add('hidden');
    // Reset so selecting the same file again still fires onchange.
    if (input) input.value = '';
  }
}

async function removeProfilePhoto() {
  const removeBtn = document.getElementById('profile-photo-remove-btn');
  removeBtn.disabled = true;

  try {
    const res = await fetch('/api/v1/staff/photo', {
      method: 'DELETE',
      headers: {
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
      },
    });

    const data = await res.json().catch(() => ({}));

    if (res.status === 401) {
      showSessionTimeoutModal();
      return;
    }

    if (!res.ok) {
      showToast(data.error || "Couldn't remove photo. Please try again.", 'error');
      return;
    }

    renderAvatars(null);
    showToast('Profile photo removed', 'success');
  } catch (err) {
    showToast('Network error - check your connection and try again.', 'error');
  } finally {
    removeBtn.disabled = false;
  }
}

function markAllNotificationsRead() {
  fetch('/api/v1/notifications/mark-all-read', {
    method: 'POST',
    headers: {
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    },
  }).then((res) => {
    if (res.ok) {
      window.location.reload();
    } else if (res.status === 401) {
      showSessionTimeoutModal();
    } else {
      showToast("Couldn't mark notifications as read", 'error');
    }
  }).catch(() => showToast("Couldn't mark notifications as read", 'error'));
}

// Per-item dismiss from the bell dropdown - the small '×' next to each
// row. Reloads on success (same as every other notification/staff action
// in this app that needs a sidebar badge count to reflect immediately)
// rather than just removing the row client-side, so the bell's unread
// count updates too, not just the open dropdown's contents.
function dismissBellNotification(id) {
  fetch(`/api/v1/notifications/${id}/clear`, {
    method: 'POST',
    headers: {
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    },
  }).then((res) => {
    if (res.ok) {
      window.location.reload();
    } else if (res.status === 401) {
      showSessionTimeoutModal();
    } else {
      showToast("Couldn't clear notification", 'error');
    }
  }).catch(() => showToast("Couldn't clear notification", 'error'));
}

// The notification-settings modal (routes/staff.py: update_notification_
// settings) swaps an empty body into #global-modal-container on a
// successful save - that's what actually closes it (see that route's
// docstring), but it leaves no on-screen confirmation. This delegated
// listener (the form is loaded in dynamically, so it can't have its own
// listener attached at page load) fills that in: an empty response body
// means the save succeeded and the modal just closed; a non-empty one
// means it re-rendered with a validation error already shown inline, so
// no separate toast is needed there.
document.body.addEventListener('htmx:afterRequest', function (evt) {
  if (evt.detail.elt && evt.detail.elt.id === 'notification-settings-form' && evt.detail.successful) {
    if ((evt.detail.xhr.responseText || '').trim() === '') {
      showToast('Notification settings updated', 'success');
    }
  }
});

// Inventory/Members/Staff's barcode-scan-to-Enter shortcut (asset-search /
// member-search / staff-search, see each page's own <script>) renders
// _barcode_not_found_modal.html into #modal-container on a miss - the
// modal itself already names the scanned barcode, so once it's up the
// search box can just be cleared outright for the next scan, same as
// every other barcode field in the app. `evt.detail.elt` resolves to the
// search input because each page's htmx.ajax call passes `source: e.target`
// - not a hardcoded id, so this one listener covers all three pages.
document.body.addEventListener('htmx:afterSwap', function (evt) {
  if (evt.detail.target?.id !== 'modal-container') return;
  if (!document.getElementById('barcode-not-found-modal')) return;
  clearInputAfterError(evt.detail.elt);
});

function updateNotificationPref(field, checked) {
  const toggleIds = {
    notify_due_soon: 'notify-due-soon-toggle',
    notify_overdue: 'notify-overdue-toggle',
    notify_requests: 'notify-requests-toggle',
  };
  const toggleEl = document.getElementById(toggleIds[field]);

  fetch('/api/v1/staff/notification-prefs', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    },
    body: JSON.stringify({ [field]: checked }),
  }).then((res) => {
    if (res.status === 401) {
      showSessionTimeoutModal();
    } else if (!res.ok) {
      // revert the toggle - the save didn't take
      if (toggleEl) toggleEl.checked = !checked;
      showToast("Couldn't save that preference", 'error');
    }
  }).catch(() => {
    if (toggleEl) toggleEl.checked = !checked;
    showToast("Couldn't save that preference", 'error');
  });
}

// Same shape as updateNotificationPref above, for the toast-duration
// number input - note the newly-saved duration only takes effect for
// toasts shown *after* a reload (TOAST_DEFAULT_DURATION_MS/
// TOAST_ACTION_DURATION_MS below are computed once at page-render time),
// not the confirmation toast this very call triggers.
function updateToastDuration(value) {
  const seconds = parseInt(value, 10);
  if (isNaN(seconds) || seconds < 2 || seconds > 15) {
    showToast('Toast duration must be between 2 and 15 seconds', 'error');
    return;
  }

  fetch('/api/v1/staff/notification-prefs', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    },
    body: JSON.stringify({ toast_duration_seconds: seconds }),
  }).then((res) => {
    if (res.status === 401) {
      showSessionTimeoutModal();
    } else if (!res.ok) {
      showToast("Couldn't save that preference", 'error');
    } else {
      showToast('Toast duration saved - applies after your next page load', 'success');
    }
  }).catch(() => {
    showToast("Couldn't save that preference", 'error');
  });
}

function setNotifTab(tab) {
  document.querySelectorAll('.notif-tab').forEach((btn) => {
    const active = btn.id === `notif-tab-${tab}`;
    btn.classList.toggle('notif-tab-active', active);
    btn.classList.toggle('text-base-content/60', !active);
    btn.classList.toggle('hover:bg-base-200', !active);
  });

  const rows = document.querySelectorAll('#notif-list .notif-row');
  let visibleCount = 0;
  rows.forEach((row) => {
    const show = tab === 'all' || row.dataset.unread === 'true';
    row.classList.toggle('hidden', !show);
    if (show) visibleCount += 1;
  });

  const hasAnyRows = rows.length > 0;
  document.getElementById('notif-empty-all').classList.toggle('hidden', tab !== 'all' || hasAnyRows);
  document.getElementById('notif-empty-unread').classList.toggle('hidden', tab !== 'unread' || visibleCount > 0);
}

// Three modes - see the head boot script's comment at the top of
// this file for the full reasoning. Mixed and Light share
// data-theme="tesda" (same color tokens throughout) and differ only in
// data-sidebar, which the CSS overrides further up this file key off
// of. Dark is a fully separate theme (data-theme="tesda-dark"). Cycling
// order (Mixed -> Light -> Dark -> Mixed -> ...) keeps the old default
// first and Dark right after it, so an existing saved "tesda"/"tesda-dark"
// preference (mapped to 'mixed'/'dark' below) lands in the exact same
// visual spot it always has. (Modern Dark, a former 4th mode, was
// removed per feedback.)
const THEME_MODES = ['mixed', 'light', 'dark'];
const THEME_MODE_ATTRS = {
  mixed: { theme: 'tesda', sidebar: 'dark' },
  light: { theme: 'tesda', sidebar: 'light' },
  dark: { theme: 'tesda-dark', sidebar: 'dark' },
};
const THEME_MODE_LABELS = { mixed: 'Mixed Mode', light: 'Light Mode', dark: 'Dark Mode' };
const THEME_MODE_ICONS = {
  // Contrast glyph (half-filled circle) - distinct from the plain sun/moon
  // below, reads as "part light, part dark" at a glance.
  mixed: '<svg xmlns="http://www.w3.org/2000/svg" class="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 2a10 10 0 000 20z" fill="currentColor" stroke="none"/></svg>',
  light: '<svg xmlns="http://www.w3.org/2000/svg" class="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  dark: '<svg xmlns="http://www.w3.org/2000/svg" class="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>',
};

const STAFF_KEY = window.APP_CONFIG.staffKey;

function currentThemeMode() {
  const theme = document.documentElement.getAttribute('data-theme');
  const sidebar = document.documentElement.getAttribute('data-sidebar');
  if (theme === 'tesda-dark') return 'dark';
  if (theme === 'tesda' && sidebar === 'light') return 'light';
  return 'mixed';
}

let THEME_MODE = currentThemeMode();

// Same light/dark logo asset pairing as the sidebar wordmark and the
// early-paint favicon script in base.html's <head> - kept as one small
// lookup here since url_for isn't available from a static .js file, unlike
// that inline <script>.
const FAVICON_HREFS = {
  light: '/static/images/logo-icon-light.png',
  dark: '/static/images/logo-icon-dark.png',
};

// Driven by the BROWSER/OS's own light-or-dark preference, not the app's
// Theme mode - see base.html's identical early-paint version for the
// full reasoning (the tab favicon is browser chrome, it should match the
// OS/browser's actual visual context, not whatever content theme is
// picked inside the app). No longer takes a theme argument - it never
// varies with app theme at all now.
function setFaviconForOsScheme() {
  const link = document.getElementById('favicon-link');
  if (!link) return;
  let isDark = false;
  try {
    isDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  } catch (e) {}
  link.href = isDark ? FAVICON_HREFS.dark : FAVICON_HREFS.light;
}

// Keeps the tab icon in sync live if the OS theme changes while this tab
// stays open, without needing a reload - base.html's own inline copy only
// runs once, at first paint.
try {
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', setFaviconForOsScheme);
} catch (e) {}

function applyThemeMode(mode) {
  const attrs = THEME_MODE_ATTRS[mode];
  document.documentElement.setAttribute('data-theme', attrs.theme);
  document.documentElement.setAttribute('data-sidebar', attrs.sidebar);
}

function syncThemeModeLabel() {
  const label = document.getElementById('theme-mode-label');
  if (label) label.textContent = THEME_MODE_LABELS[THEME_MODE];
  const icon = document.getElementById('theme-mode-icon');
  if (icon) icon.innerHTML = THEME_MODE_ICONS[THEME_MODE];
  document.querySelectorAll('.theme-mode-option').forEach(function (opt) {
    const check = opt.querySelector('.theme-mode-check');
    if (check) check.classList.toggle('hidden', opt.dataset.mode !== THEME_MODE);
  });
}
syncThemeModeLabel();

// List-and-click picker, shared by Theme mode and Notification style
// below: a click opens the option list as a floating panel, a click on
// an option selects it directly and closes the panel again.
//
// The panel is relocated to a direct child of <body> the first time it
// opens and positioned with `position: fixed` from its toggle button's
// own getBoundingClientRect() - same technique the shared #app-tooltip
// bubble elsewhere in this file already uses. This isn't just "floating
// instead of inline" for its own sake: an earlier version kept the panel
// in place with plain `position: absolute` (containing block = an
// intermediate `position: relative` wrapper, exactly as intended), and
// that was *still* enough to grow #display-accessibility-modal's
// `.modal-box` own auto height while the panel was open, because
// modal-box has `overflow-y: auto` and Chromium factors an
// absolutely-positioned descendant's extent into that box's auto-height
// even when its containing block is a nearer ancestor. Moving the panel
// fully outside the scrolling box sidesteps that instead of fighting it.
const INLINE_DROPDOWN_TOGGLE_BTN = {
  'theme-mode-menu': 'theme-mode-toggle-btn',
  'toast-style-menu': 'toast-style-toggle-btn',
};

function toggleInlineDropdown(menuId, chevronId) {
  const menu = document.getElementById(menuId);
  if (!menu) return;
  const opening = menu.classList.contains('hidden');

  // Only one of these floating panels open at a time - opening one
  // closes any other that's already open, same as a native <select>.
  document.querySelectorAll('.inline-dropdown-menu').forEach(function (m) {
    if (m === menu) return;
    m.classList.add('hidden');
    const otherBtnId = INLINE_DROPDOWN_TOGGLE_BTN[m.id];
    const otherChevron = otherBtnId && document.getElementById(otherBtnId)?.querySelector('svg[id$="-chevron"]');
    if (otherChevron) otherChevron.style.transform = '';
  });

  if (opening) {
    const btn = document.getElementById(INLINE_DROPDOWN_TOGGLE_BTN[menuId]);
    if (btn) {
      // Appended to the enclosing <dialog>, not document.body: a native
      // <dialog> opened via showModal() is promoted to the browser's top
      // layer and always paints above ordinary page content regardless of
      // z-index (same quirk noted for #checkout-modal-container above). A
      // plain document.body child falls outside that top layer, so it was
      // rendering *behind* this modal instead of above it. Keeping the
      // panel inside the dialog preserves that top-layer promotion while
      // still escaping .modal-box's overflow (the original reason it's
      // relocated at all - see the comment above this function).
      (btn.closest('dialog') || document.body).appendChild(menu);
      const rect = btn.getBoundingClientRect();
      menu.style.position = 'fixed';
      menu.style.top = (rect.bottom + 4) + 'px';
      menu.style.left = rect.left + 'px';
      menu.style.width = rect.width + 'px';
    }
  }

  menu.classList.toggle('hidden', !opening);
  const chevron = chevronId ? document.getElementById(chevronId) : null;
  if (chevron) chevron.style.transform = opening ? 'rotate(180deg)' : '';
}

// Closes any open floating dropdown panel on an outside click - same
// "click away to dismiss" behavior a native <select> gets for free.
// Once relocated to <body>, a panel is no longer near its toggle button
// in the DOM, so "outside" is checked against the button directly
// (tracked via INLINE_DROPDOWN_TOGGLE_BTN) rather than a shared wrapper.
document.addEventListener('click', function (e) {
  document.querySelectorAll('.inline-dropdown-menu:not(.hidden)').forEach(function (menu) {
    if (menu.contains(e.target)) return;
    const btn = document.getElementById(INLINE_DROPDOWN_TOGGLE_BTN[menu.id]);
    if (btn && btn.contains(e.target)) return;
    menu.classList.add('hidden');
    const chevron = btn?.querySelector('svg[id$="-chevron"]');
    if (chevron) chevron.style.transform = '';
  });
});

function setThemeMode(mode) {
  if (!THEME_MODE_ATTRS[mode]) return;
  THEME_MODE = mode;
  applyThemeMode(THEME_MODE);
  try {
    localStorage.setItem('app-theme:' + STAFF_KEY, THEME_MODE);
  } catch (e) {}
  syncThemeModeLabel();
  toggleInlineDropdown('theme-mode-menu', 'theme-mode-chevron');
  showToast(`Theme mode set to ${THEME_MODE_LABELS[THEME_MODE]}`, 'success');
}

const TOAST_STYLES = ['light', 'inverted', 'md3'];
const TOAST_STYLE_LABELS = { light: 'Light Mode', inverted: 'Dark Mode', md3: 'Material Design' };
const TOAST_STYLE_ICONS = {
  light: '<svg xmlns="http://www.w3.org/2000/svg" class="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>',
  inverted: '<svg xmlns="http://www.w3.org/2000/svg" class="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/></svg>',
  md3: '<svg xmlns="http://www.w3.org/2000/svg" class="w-[18px] h-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22c-5.5 0-10-4.5-10-10S6.5 2 12 2s10 4 10 9a3.5 3.5 0 01-3.5 3.5h-1.8a2 2 0 00-1 3.7A2 2 0 0114 22z"/><circle cx="7.5" cy="10.5" r="1" fill="currentColor" stroke="none"/><circle cx="12" cy="7.5" r="1" fill="currentColor" stroke="none"/><circle cx="16.5" cy="10.5" r="1" fill="currentColor" stroke="none"/></svg>',
};

// Material Design is the app-wide default toast style, same per-account
// override rule as THEME_MODE above (keyed by username, never leaks
// between accounts on a shared browser).
let TOAST_STYLE = 'md3';
try {
  const saved = localStorage.getItem('toast-style:' + STAFF_KEY);
  if (TOAST_STYLES.includes(saved)) TOAST_STYLE = saved;
} catch (e) {}

function syncToastStyleLabel() {
  const label = document.getElementById('toast-style-label');
  if (label) label.textContent = TOAST_STYLE_LABELS[TOAST_STYLE];
  const icon = document.getElementById('toast-style-icon');
  if (icon) icon.innerHTML = TOAST_STYLE_ICONS[TOAST_STYLE];
  document.querySelectorAll('.toast-style-option').forEach(function (opt) {
    const check = opt.querySelector('.toast-style-check');
    if (check) check.classList.toggle('hidden', opt.dataset.style !== TOAST_STYLE);
  });
}
syncToastStyleLabel();

// List-and-click, same as Theme mode above - replaces the old
// cycle-through-on-click toggle (see toggleInlineDropdown's comment).
function setToastStyle(style) {
  if (!TOAST_STYLES.includes(style)) return;
  TOAST_STYLE = style;
  try {
    localStorage.setItem('toast-style:' + STAFF_KEY, TOAST_STYLE);
  } catch (e) {}
  syncToastStyleLabel();
  toggleInlineDropdown('toast-style-menu', 'toast-style-chevron');
  showToast(`Notification style set to ${TOAST_STYLE_LABELS[TOAST_STYLE]}`, 'success');
}

// Per-staff preference (staff.toast_duration_seconds, migrations/0038,
// routes/staff.py's update_own_notification_prefs, the Settings &
// privacy modal above) - "if staff else" rather than "staff.toast_...
// or 4" since this script runs outside the Jinja "staff is truthy" guard
// further up the page (e.g. an anonymous /login render), and unlike a
// plain "or" expression, Jinja's inline if/else never evaluates the
// untaken branch, so staff.toast_duration_seconds is never touched when
// staff itself is undefined. The action-toast bonus stays a flat +2s on
// top, same gap as the old hardcoded 4000/6000 pair.
const TOAST_DEFAULT_DURATION_MS = window.APP_CONFIG.toastDurationMs;
const TOAST_ACTION_DURATION_MS = TOAST_DEFAULT_DURATION_MS + 2000;

const SESSION_TIMEOUT_REDIRECT_SECONDS = 3;

function showSessionTimeoutModal() {
  const modal = document.getElementById('session-timeout-modal');
  // Guards against every other 401 handler on the page calling this --
  // there are several, one per fetch() call scattered across templates
  // plus the global htmx:responseError listener - re-triggering the
  // countdown/redirect timer on an already-open modal.
  if (!modal || modal.open) return;
  modal.showModal();

  let secondsLeft = SESSION_TIMEOUT_REDIRECT_SECONDS;
  const countdownEl = document.getElementById('session-timeout-countdown');
  if (countdownEl) countdownEl.textContent = String(secondsLeft);

  const interval = setInterval(function () {
    secondsLeft -= 1;
    if (countdownEl) countdownEl.textContent = String(Math.max(secondsLeft, 0));
    if (secondsLeft <= 0) {
      clearInterval(interval);
      window.location.href = '/login';
    }
  }, 1000);
}
(function () {
  const modal = document.getElementById('session-timeout-modal');
  if (modal) modal.addEventListener('cancel', (e) => e.preventDefault());
})();

// Global RFID exit-monitoring alarm - polls from every logged-in page (not
// just Monitoring itself) so an unauthorized/unknown-tag detection anywhere
// in the app interrupts with the same full-screen modal
// (templates/base.html's #global-alarm-modal). Same idea, unauthenticated
// version lives in templates/login.html for someone not yet logged in.
//
// "Episode"-based, not "poll-based": the modal shows once when the alarm
// transitions inactive -> active, and Dismiss hides it for the rest of that
// same active stretch (polling continues regardless, so it still auto-hides
// the moment the Admin's Master Key Card tap clears it) - without this, a
// dismissed alarm would just pop back up on the very next 5s poll.
let globalAlarmWasActive = false;
let globalAlarmDismissedForThisEpisode = false;

function dismissGlobalAlarmModal() {
  globalAlarmDismissedForThisEpisode = true;
  document.getElementById('global-alarm-modal')?.close();
}

// Sidebar "Monitoring" badge - updated from the same pollGlobalAlarm() poll
// below rather than its own separate poll loop, so a NEW alarm shows up
// here within 5s even while sitting on some other tab, not just after the
// next full page load. Both spans are always in the DOM (base.html), just
// hidden/shown here - see that markup's own comment for why. count 0/falsy
// hides both; a real count shows it (capped at "9+" display, same as
// Notifications/Feedback's badges) with the pulse CSS already on the class.
function updateSidebarMonitoringBadge(count) {
  const collapsed = document.getElementById('sidebar-monitoring-badge-collapsed');
  const expanded = document.getElementById('sidebar-monitoring-badge-expanded');
  if (!collapsed && !expanded) return; // role doesn't have the Monitoring link at all
  const text = count > 9 ? '9+' : String(count || 0);
  [collapsed, expanded].forEach(function (el) {
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('hidden', !count);
  });
}

async function pollGlobalAlarm() {
  const modal = document.getElementById('global-alarm-modal');
  if (!modal) return;

  try {
    const res = await fetch('/api/v1/monitoring/alarm-status');
    if (!res.ok) return; // best-effort - a hiccup here shouldn't itself interrupt anyone
    const data = await res.json();

    if (data.active) {
      if (!globalAlarmWasActive) globalAlarmDismissedForThisEpisode = false; // new episode
      globalAlarmWasActive = true;
      updateSidebarMonitoringBadge(data.active_count);

      const detailEl = document.getElementById('global-alarm-detail');
      if (detailEl) {
        const who = data.asset_name
          ? `${data.asset_name} (${data.asset_barcode || '?'})`
          : 'An unregistered tag';
        const countNote = data.active_count > 1 ? ` - ${data.active_count} events` : '';
        detailEl.textContent = `${who} was detected leaving without an authorized checkout.${countNote}`;
      }

      // Never on the Monitoring page itself - its own alarm banner already
      // gives this full prominent treatment there; a second interrupt on
      // top of it would just be noise for someone already looking at it.
      const onMonitoringPage = window.location.pathname === '/monitoring';
      if (!modal.open && !globalAlarmDismissedForThisEpisode && !onMonitoringPage) {
        modal.showModal();
      }
    } else {
      globalAlarmWasActive = false;
      globalAlarmDismissedForThisEpisode = false;
      updateSidebarMonitoringBadge(0);
      if (modal.open) modal.close();
    }
  } catch (err) {
    // Network hiccup - next poll tries again, nothing to surface here.
  }
}

if (window.APP_CONFIG.monitoringAlarmPollEnabled) {
  pollGlobalAlarm();
  setInterval(pollGlobalAlarm, 5000);
}

const TOAST_VARIANTS = {
  success: {
    iconWrap: 'bg-emerald-100',
    icon: '<svg class="w-4 h-4 text-emerald-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>',
    action: 'text-emerald-600',
    bar: 'bg-emerald-500',
    track: 'bg-emerald-100',
  },
  error: {
    iconWrap: 'bg-red-100',
    icon: '<svg class="w-4 h-4 text-red-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    action: 'text-red-600',
    bar: 'bg-red-500',
    track: 'bg-red-100',
  },
  warning: {
    iconWrap: 'bg-amber-100',
    icon: '<span class="w-5 h-5 rounded-full bg-amber-500 flex items-center justify-center"><svg class="w-2.5 h-2.5 text-white" viewBox="0 0 24 24" fill="currentColor"><rect x="10.5" y="4" width="3" height="9" rx="1.5"/><rect x="10.5" y="15.5" width="3" height="3" rx="1.5"/></svg></span>',
    action: 'text-amber-600',
    bar: 'bg-amber-500',
    track: 'bg-amber-100',
  },
  info: {
    iconWrap: 'bg-blue-100',
    icon: '<span class="w-5 h-5 rounded-full bg-blue-500 flex items-center justify-center"><svg class="w-2.5 h-2.5 text-white" viewBox="0 0 24 24" fill="currentColor"><rect x="10.5" y="10" width="3" height="8" rx="1.5"/><circle cx="12" cy="6" r="1.75"/></svg></span>',
    action: 'text-blue-600',
    bar: 'bg-blue-500',
    track: 'bg-blue-100',
  },
};

const TOAST_VARIANTS_DARK = {
  success: {
    iconWrap: 'bg-emerald-400/20',
    icon: '<svg class="w-4 h-4 text-emerald-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>',
    action: 'text-emerald-300',
    bar: 'bg-emerald-300',
    track: 'bg-white/10',
  },
  error: {
    iconWrap: 'bg-red-400/20',
    icon: '<svg class="w-4 h-4 text-red-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    action: 'text-red-300',
    bar: 'bg-red-300',
    track: 'bg-white/10',
  },
  warning: {
    iconWrap: 'bg-amber-400/20',
    icon: '<span class="w-5 h-5 rounded-full bg-amber-400/90 flex items-center justify-center"><svg class="w-2.5 h-2.5 text-[#313033]" viewBox="0 0 24 24" fill="currentColor"><rect x="10.5" y="4" width="3" height="9" rx="1.5"/><rect x="10.5" y="15.5" width="3" height="3" rx="1.5"/></svg></span>',
    action: 'text-amber-300',
    bar: 'bg-amber-300',
    track: 'bg-white/10',
  },
  info: {
    iconWrap: 'bg-blue-400/20',
    icon: '<span class="w-5 h-5 rounded-full bg-blue-400/90 flex items-center justify-center"><svg class="w-2.5 h-2.5 text-[#313033]" viewBox="0 0 24 24" fill="currentColor"><rect x="10.5" y="10" width="3" height="8" rx="1.5"/><circle cx="12" cy="6" r="1.75"/></svg></span>',
    action: 'text-blue-300',
    bar: 'bg-blue-300',
    track: 'bg-white/10',
  },
};

// Shared by every barcode-scan textbox in the app (checkout cart, return
// cart, member-barcode fields, the Inventory/Members/Staff search boxes) --
// on any scan error the field is cleared outright (not just selected) so
// the next scan/keystroke starts clean; the error message itself is what
// now carries the barcode that was scanned (see each caller's backend
// error text), so there's no need to leave the bad value on screen to
// reference.
function clearInputAfterError(input) {
  if (!input) return;
  input.value = '';
  input.focus();
}

// Briefly highlights an element - used to point at a cart/checklist row
// that a rescanned barcode turned out to already match (already in the
// checkout cart, already checked off on a return), so staff can see *which*
// row it was without hunting the list themselves. Driven entirely by
// inline styles rather than a stylesheet class/keyframe, so it doesn't
// depend on static/css/output.css having been rebuilt.
function flashHighlight(el, ms) {
  if (!el) return;
  el.scrollIntoView({ block: 'nearest' });
  const prevTransition = el.style.transition;
  const prevBg = el.style.backgroundColor;
  const prevShadow = el.style.boxShadow;
  el.style.transition = 'background-color 0.25s ease, box-shadow 0.25s ease';
  el.style.backgroundColor = 'rgba(67, 160, 71, 0.45)'; // success-green, theme-independent on purpose
  // inset (not an outer 0 0 0 2px ring) - an outer shadow's spread extends
  // past the element's own edges, so on a rounded-corner row (cart items,
  // etc.) it visibly overshoots the box and squares off the corners since
  // the extra 2px ring sits outside the radius. Inset draws the ring
  // entirely within the border box instead, so it always exactly matches
  // the element's own size and corner radius, whatever that is.
  el.style.boxShadow = 'inset 0 0 0 2px rgba(67, 160, 71, 0.65)';
  setTimeout(() => {
    el.style.backgroundColor = prevBg;
    el.style.boxShadow = prevShadow;
    setTimeout(() => { el.style.transition = prevTransition; }, 300);
  }, ms || 1500);
}

function showToast(message, type, action, duration, description) {
  const container = document.getElementById('toast-container');
  if (!container || !message) return;

  if (TOAST_STYLE === 'md3') {
    renderMd3Toast(container, message, type, action, duration, description);
    return;
  }

  const variantMap = TOAST_STYLE === 'inverted' ? TOAST_VARIANTS_DARK : TOAST_VARIANTS;
  const variant = variantMap[type] || variantMap.success;

  const isSnackbar = !!(action && action.label);

  const toast = document.createElement('div');
  toast.className = `relative ${isSnackbar ? 'w-[310px]' : 'w-72'} ${TOAST_STYLE === 'inverted' ? 'bg-[#313033]' : 'bg-white'} rounded-lg ${isSnackbar ? 'shadow-xl' : 'shadow-lg'} ring-1 ${TOAST_STYLE === 'inverted' ? 'ring-white/10' : 'ring-black/5'} overflow-hidden
                      pointer-events-auto transition-all duration-300 ease-out opacity-0 translate-x-6`;

  const body = document.createElement('div');
  body.className = 'flex items-center gap-2.5 p-3';

  const iconWrap = document.createElement('span');
  iconWrap.className = `w-8 h-8 rounded-lg shrink-0 flex items-center justify-center ${variant.iconWrap}`;
  iconWrap.innerHTML = variant.icon;
  body.appendChild(iconWrap);

  const content = document.createElement('div');
  content.className = 'flex-1 min-w-0';

  const titleRow = document.createElement('div');
  titleRow.className = 'flex items-center justify-between gap-2';

  const titleEl = document.createElement('p');
  titleEl.className = `font-semibold ${TOAST_STYLE === 'inverted' ? 'text-[#F4EFF4]' : 'text-gray-900'} text-[13px] leading-snug`;
  titleEl.textContent = message;
  titleRow.appendChild(titleEl);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Dismiss');
  closeBtn.className = `shrink-0 -mr-1 p-1 rounded-full transition-colors ${TOAST_STYLE === 'inverted' ? 'text-white/40 hover:text-white/80 hover:bg-white/10' : 'text-gray-400 hover:text-gray-600 hover:bg-black/5'}`;
  closeBtn.innerHTML = '<svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  titleRow.appendChild(closeBtn);

  content.appendChild(titleRow);

  if (description) {
    const descEl = document.createElement('p');
    descEl.className = `${TOAST_STYLE === 'inverted' ? 'text-white/60' : 'text-gray-500'} text-xs mt-0.5 leading-snug`;
    descEl.textContent = description;
    content.appendChild(descEl);
  }

  let dismissTimer;
  function dismiss() {
    clearTimeout(dismissTimer);
    toast.classList.add('opacity-0', 'translate-x-6');
    setTimeout(() => toast.remove(), 300);
  }

  if (action && action.label) {
    const actionBtn = document.createElement('button');
    actionBtn.type = 'button';
    actionBtn.className = `-ml-1.5 mt-1.5 px-1.5 py-1 rounded-md font-semibold text-xs transition-colors ${variant.action} ${TOAST_STYLE === 'inverted' ? 'hover:bg-white/10' : 'hover:bg-black/5'}`;
    actionBtn.textContent = action.label;
    actionBtn.addEventListener('click', () => {
      if (action.url) {
        fetch(action.url, {
          method: action.method || 'POST',
          headers: {
            'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
          },
        }).then((res) => {
          if (res.ok) {
            if (action.successMessage) {
              sessionStorage.setItem('pending-toast', JSON.stringify({ message: action.successMessage, type: 'success' }));
            }
            window.location.reload();
          } else if (res.status === 401) {
            showSessionTimeoutModal();
          } else {
            showToast("That action couldn't be undone", 'error');
          }
        }).catch(() => showToast("That action couldn't be undone", 'error'));
      }
      dismiss();
    });
    content.appendChild(actionBtn);
  }

  body.appendChild(content);
  toast.appendChild(body);

  const track = document.createElement('div');
  track.className = `h-[2px] w-full ${variant.track}`;
  const fill = document.createElement('div');
  fill.className = `h-full ${variant.bar}`;
  fill.style.width = '100%';
  track.appendChild(fill);
  toast.appendChild(track);

  closeBtn.addEventListener('click', dismiss);

  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.classList.remove('opacity-0', 'translate-x-6');
  });

  const effectiveDuration = duration || (action ? TOAST_ACTION_DURATION_MS : TOAST_DEFAULT_DURATION_MS);
  let remainingMs = effectiveDuration;

  function runCountdown(ms) {
    if (!fill) return;
    fill.style.transition = 'none';
    fill.style.width = '100%';
    requestAnimationFrame(() => {
      fill.style.transition = `width ${ms}ms linear`;
      fill.style.width = '0%';
    });
  }
  runCountdown(remainingMs);
  dismissTimer = setTimeout(dismiss, remainingMs);

  toast.addEventListener('mouseenter', () => {
    clearTimeout(dismissTimer);
    if (track && fill) {
      const trackWidth = track.getBoundingClientRect().width;
      const fillWidth = fill.getBoundingClientRect().width;
      const fraction = trackWidth > 0 ? fillWidth / trackWidth : 0;
      remainingMs = Math.round(remainingMs * fraction);
      fill.style.transition = 'none';
      fill.style.width = `${fraction * 100}%`;
    }
  });
  toast.addEventListener('mouseleave', () => {
    if (remainingMs <= 0) return;
    runCountdown(remainingMs);
    dismissTimer = setTimeout(dismiss, remainingMs);
  });
}

function renderMd3Toast(container, message, type, action, duration, description) {
  const hasAction = !!(action && action.label);

  const toast = document.createElement('div');
  toast.setAttribute('role', 'status');
  toast.className = `relative flex items-center gap-2 min-w-[344px] max-w-[420px] rounded-[4px]
                      bg-[#322F35] pl-4 pr-2 py-2 pointer-events-auto
                      shadow-[0_1px_3px_rgba(0,0,0,0.3),0_4px_8px_rgba(0,0,0,0.15)]
                      transition-all duration-300 ease-out opacity-0 translate-y-2`;

  const textWrap = document.createElement('div');
  textWrap.className = 'flex-1 min-w-0 py-1.5';

  const msgEl = document.createElement('p');
  msgEl.className = 'text-[#F5EFF7] text-sm leading-5 font-normal';
  msgEl.textContent = message;
  textWrap.appendChild(msgEl);

  if (description) {
    const descEl = document.createElement('p');
    descEl.className = 'text-[#F5EFF7]/70 text-xs leading-4 mt-0.5';
    descEl.textContent = description;
    textWrap.appendChild(descEl);
  }
  toast.appendChild(textWrap);

  let dismissTimer;
  function dismiss() {
    clearTimeout(dismissTimer);
    toast.classList.add('opacity-0', 'translate-y-2');
    setTimeout(() => toast.remove(), 300);
  }

  if (hasAction) {
    const actionBtn = document.createElement('button');
    actionBtn.type = 'button';
    actionBtn.className = 'shrink-0 text-[#1565C0] text-sm font-medium leading-5 px-2.5 py-1.5 rounded hover:bg-white/10';
    actionBtn.textContent = action.label;
    actionBtn.addEventListener('click', () => {
      if (action.url) {
        fetch(action.url, {
          method: action.method || 'POST',
          headers: { 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content },
        }).then((res) => {
          if (res.ok) {
            if (action.successMessage) {
              sessionStorage.setItem('pending-toast', JSON.stringify({ message: action.successMessage, type: 'success' }));
            }
            window.location.reload();
          } else if (res.status === 401) {
            showSessionTimeoutModal();
          } else {
            showToast("That action couldn't be undone", 'error');
          }
        }).catch(() => showToast("That action couldn't be undone", 'error'));
      }
      dismiss();
    });
    toast.appendChild(actionBtn);
  }

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', 'Dismiss');
  closeBtn.className = 'shrink-0 p-1.5 text-[#F5EFF7]/60 hover:text-[#F5EFF7] hover:bg-white/10 rounded-full';
  closeBtn.innerHTML = '<svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  closeBtn.addEventListener('click', dismiss);
  toast.appendChild(closeBtn);

  container.appendChild(toast);

  requestAnimationFrame(() => {
    toast.classList.remove('opacity-0', 'translate-y-2');
  });

  const effectiveDuration = duration || (hasAction ? TOAST_ACTION_DURATION_MS : TOAST_DEFAULT_DURATION_MS);
  let remainingMs = effectiveDuration;
  let pauseStart = Date.now();
  dismissTimer = setTimeout(dismiss, remainingMs);

  toast.addEventListener('mouseenter', () => {
    clearTimeout(dismissTimer);
    remainingMs -= (Date.now() - pauseStart);
  });
  toast.addEventListener('mouseleave', () => {
    if (remainingMs <= 0) return;
    pauseStart = Date.now();
    dismissTimer = setTimeout(dismiss, remainingMs);
  });
}

document.body.addEventListener('show-toast', function (evt) {
  const detail = evt.detail || {};
  showToast(detail.message, detail.type, detail.action, detail.duration);
});

document.body.addEventListener('htmx:responseError', function (evt) {
  // Passive/ambient regions (e.g. a background banner check on page load)
  // refresh themselves without the user asking, so a failure there
  // shouldn't interrupt them with an error toast - it should just stay
  // quiet and try again next time its trigger fires. Still 401-aware, since
  // a real session timeout matters everywhere, even in passive regions.
  const status = evt.detail && evt.detail.xhr ? evt.detail.xhr.status : null;
  if (status === 401) {
    showSessionTimeoutModal();
    return;
  }
  if (evt.target && evt.target.closest('[data-passive-region]')) {
    console.warn('Passive region request failed:', evt.detail);
    return;
  }
  const message = status === 403
    ? "You don't have permission to do that."
    : "Something went wrong. Please try again.";
  showToast(message, 'error');
});
document.body.addEventListener('htmx:sendError', function (evt) {
  if (evt.target && evt.target.closest('[data-passive-region]')) {
    console.warn('Passive region request failed to send:', evt.detail);
    return;
  }
  showToast("Network error - check your connection and try again.", 'error');
});

// crypto.randomUUID() only exists in a "secure context" (HTTPS, or exactly
// `localhost`) - on a LAN deployment reached over plain http://<lan-ip> (the
// default setup per DEPLOYMENT.md, before/without the optional Caddy HTTPS
// step) it's simply undefined, which throws for every caller below.
// crypto.getRandomValues() has no such restriction, so fall back to building
// a UUID v4 from that instead of Math.random() (which would still work, just
// with much weaker randomness for something that's only ever a client-side
// session-tracking id anyway). Attached to window for the same reason
// Checkout is below - htmx's "js:" hx-vals expressions run through
// `new Function(...)`, which only sees true globals.
window.generateClientId = function () {
  if (window.crypto && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10xx
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0'));
  return `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`;
};

// Checkout widget controller. A staff member can have several checkout sessions open at once
// (one minimized while checking out a second trainee), so every instance - a dialog in
// #checkout-modal-container plus its dock in #checkout-docks-tray - gets its own data-client-id
// and its state lives in this module's `state` map, keyed by that id.
// Attached to window (not just `const Checkout = ...`) because htmx's "js:" hx-vals expressions
// run through `new Function(...)`, which can only see true globals - a plain const would be
// invisible to hx-trigger="click[Checkout.countOk()]" even though onclick="" can see it fine.
window.Checkout = (function () {
  const state = new Map();
  const MAX_CONCURRENT_CHECKOUTS = 4;
  // Which instance the shared #checkout-close-confirm-modal is currently
  // asking about - set by requestClose() right before showing it, read
  // by confirmCloseFromDock()/cancelCloseConfirm() below.
  let pendingCloseClientId = null;

  function csrfHeaders() {
    return {
      'Content-Type': 'application/json',
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
    };
  }

  function clientIdOf(el) {
    const root = el.closest('[data-client-id]');
    return root ? root.dataset.clientId : null;
  }

  function dialogOf(clientId) {
    return document.getElementById('checkout-modal-' + clientId);
  }

  function dockOf(clientId) {
    return document.getElementById('checkout-dock-' + clientId);
  }

  function fieldOf(clientId, role) {
    const dialog = dialogOf(clientId);
    return dialog ? dialog.querySelector('[data-role="' + role + '"]') : null;
  }

  function cartItemCount(clientId) {
    const list = fieldOf(clientId, 'cart-list');
    return list ? list.children.length : 0;
  }

  // Finds the cart row for a rescanned-and-already-there asset and flashes
  // it via the global flashHighlight() - see that function (near
  // showToast, above) for why it's inline-style-driven rather than a CSS
  // class.
  function flashCartItem(clientId, itemId) {
    if (!itemId) return;
    const li = fieldOf(clientId, 'cart-list')?.querySelector(`[data-item-id="${CSS.escape(String(itemId))}"]`);
    flashHighlight(li);
  }

  function isMinimized(clientId) {
    const dock = dockOf(clientId);
    return !!dock && !dock.classList.contains('hidden');
  }

  function updateDockLabel(clientId) {
    const dockLabel = dockOf(clientId)?.querySelector('[data-role="dock-label"]');
    if (!dockLabel) return;
    const s = state.get(clientId);
    const count = cartItemCount(clientId);
    dockLabel.textContent = (s && s.memberFirstName)
      ? `${s.memberFirstName} \u00b7 ${s.memberBarcode} \u00b7 ${count} item${count === 1 ? '' : 's'}`
      : 'Checkout';
  }

  function openInstanceCount() {
    return document.querySelectorAll('#checkout-modal-container > dialog.checkout-modal').length;
  }

  // Keeps body.checkout-panel-open in sync with "is any checkout dialog
  // currently expanded" - the #checkout-docks-tray CSS above reads this
  // class to shift itself clear of the panel only while one's actually
  // open, rather than permanently relocating the tray. Safe to call
  // after any state change (expand, minimize, cancel) since at most one
  // checkout dialog is ever open at a time.
  function syncPanelOpenClass() {
    const anyOpen = !!document.querySelector('#checkout-modal-container > dialog.checkout-modal[open]');
    document.body.classList.toggle('checkout-panel-open', anyOpen);
  }

  return {
    // Registers a freshly-rendered instance's starting state. Called once
    // by the inline <script> at the bottom of every _checkout_modal.html
    // fragment, whether that's a brand-new checkout, a resumed one, or one
    // of possibly several restored (minimized) on page load.
    init(clientId, sessionId, memberFirstName, memberBarcode) {
      state.set(clientId, {
        sessionId: sessionId || null,
        completedCount: 0,
        memberFirstName: memberFirstName || null,
        memberBarcode: memberBarcode || null,
        // Holds the first scan of a barcode-then-RFID pair while awaiting
        // the (optional) second scan - see addItem()/skipRfidVerify().
        pendingAssetCode: null,
      });
      if (isMinimized(clientId)) updateDockLabel(clientId);
    },

    // Soft cap so the dock tray (and the list of open sessions on the
    // backend) can't grow without bound if someone keeps hitting "New
    // Checkout" without ever finishing one.
    countOk() {
      if (openInstanceCount() < MAX_CONCURRENT_CHECKOUTS) return true;
      showToast(`You already have ${MAX_CONCURRENT_CHECKOUTS} checkouts open - finish or close one first.`, 'warning');
      return false;
    },

    async startSession(btn) {
      const clientId = clientIdOf(btn);
      const input = fieldOf(clientId, 'member-barcode-input');
      const errorEl = fieldOf(clientId, 'member-error');
      const barcode = input.value.trim();
      errorEl.classList.add('hidden');
      if (!barcode) return;

      // Typing/scanning a barcode directly and hitting Start is a separate
      // path from picking a row in the browse panel (selectAvailableMember
      // already closes it before calling in here) - close it here too, so
      // it doesn't linger open over step-cart once the session starts.
      fieldOf(clientId, 'available-members-panel')?.classList.add('hidden');

      try {
        const res = await fetch('/api/v1/checkout-sessions', {
          method: 'POST',
          headers: csrfHeaders(),
          body: JSON.stringify({ member_barcode: barcode }),
        });
        const data = await res.json();

        if (!res.ok) {
          if (res.status === 401) { showSessionTimeoutModal(); return; }
          if (res.status === 409 && data.checkout_session_id) {
            // This member already has a cart in progress somewhere (e.g.
            // it was minimized and forgotten, this member's badge was
            // scanned twice, or it's open in another tab) - reopen that
            // instead of dead-ending on an error or creating a duplicate.
            this._handleAlreadyInProgress(clientId, data.checkout_session_id);
            return;
          }
          errorEl.textContent = data.error || "Couldn't start checkout for this member.";
          errorEl.classList.remove('hidden');
          clearInputAfterError(input);
          return;
        }

        const s = state.get(clientId);
        s.sessionId = data.checkout_session_id;
        s.memberFirstName = data.member_first_name;
        s.memberBarcode = barcode;
        fieldOf(clientId, 'member-barcode-label').textContent = `${data.member_first_name} \u00b7 ${barcode}`;
        fieldOf(clientId, 'step-member').classList.add('hidden');
        fieldOf(clientId, 'step-cart').classList.remove('hidden');
        fieldOf(clientId, 'step-hint').textContent = 'Scan each tool being borrowed, then complete checkout.';
        fieldOf(clientId, 'complete-btn').classList.remove('hidden');
        fieldOf(clientId, 'minimize-btn').classList.remove('hidden');
        fieldOf(clientId, 'cancel-btn').textContent = 'Cancel';
        fieldOf(clientId, 'asset-code-input').focus();
      } catch (err) {
        errorEl.textContent = 'Network error - check your connection and try again.';
        errorEl.classList.remove('hidden');
        clearInputAfterError(input);
      }
    },

    _handleAlreadyInProgress(blankClientId, existingSessionId) {
      // The blank instance that triggered this never had a session of its
      // own (sessionId is still null), so tearing it down is a pure DOM
      // removal - nothing to cancel on the server.
      this._performCancel(blankClientId);
      this.resumeOrExpand(existingSessionId);
    },

    // Shared by the 409 fallback above and every "Resume" button (the
    // per-page active-checkout banner, etc). If this session is already
    // rendered in the current tab, just bring it forward - no duplicate
    // dialog, no server round-trip. Otherwise fetch it fresh and append it.
    resumeOrExpand(sessionId) {
      const existing = dialogOf(sessionId);
      if (existing) {
        this.expandById(sessionId);
        return;
      }
      if (!this.countOk()) return;
      htmx.ajax('GET', `/api/v1/checkout-sessions/${sessionId}/resume-form`, {
        target: '#checkout-modal-container',
        swap: 'beforeend',
      });
    },

    // One field, two possible Enters per item - see the template comment
    // above the input for why (preserving the barcode+RFID anti-swap check
    // from the old two-field layout without needing two visible boxes).
    // First Enter: hold the scanned value as pending, don't call the API
    // yet - there's no way to know whether an RFID follow-up scan is
    // coming. Second Enter (or skipRfidVerify()): send the pending code as
    // asset_code and whatever's in the field now (possibly nothing) as
    // rfid_code, in the exact same single add-item call the two-field
    // version always made.
    async addItem(btn) {
      const clientId = clientIdOf(btn);
      const s = state.get(clientId);
      const input = fieldOf(clientId, 'asset-code-input');
      const value = input.value.trim();

      if (!s.pendingAssetCode) {
        if (!value) return;

        // If this scan is itself the asset's RFID tag - not just its
        // barcode or asset ID code - it already proves physical possession
        // the same way a second confirmatory tap would: a barcode sticker
        // can be peeled and moved to a different tool, an embedded RFID
        // tag can't. Skip the "scan again to verify" prompt entirely in
        // that case and add it straight away, self-verified, instead of
        // asking staff to scan the exact same tag a second time for no
        // real reason. Falls through to the normal pending/second-scan
        // flow below for anything that isn't a real RFID match (a plain
        // barcode, an unresolvable code, or a network hiccup).
        if (await this._looksLikeRfidScan(value)) {
          input.value = '';
          await this._addAssetByCode(clientId, value, value);
          clearInputAfterError(input);
          return;
        }

        s.pendingAssetCode = value;
        input.value = '';
        fieldOf(clientId, 'pending-verify-hint')?.classList.remove('hidden');
        input.focus();
        return;
      }

      const assetCode = s.pendingAssetCode;
      const rfidCode = value || null;
      s.pendingAssetCode = null;
      fieldOf(clientId, 'pending-verify-hint')?.classList.add('hidden');

      // Clear + refocus either way - on success there's just nothing else
      // to show; on failure _addAssetByCode has already set the error text
      // (and flashed the matching cart row, if it was an already-in-cart hit).
      await this._addAssetByCode(clientId, assetCode, rfidCode);
      clearInputAfterError(input);
    },

    // The pending-verify hint's "add without verifying" link - same as
    // pressing Enter on the (now empty) field mid-verification, just a
    // mouse alternative for staff who scanned a barcode-only tool and
    // don't want to wait around for an RFID tap that isn't coming.
    skipRfidVerify(el) {
      const clientId = clientIdOf(el);
      this.addItem(fieldOf(clientId, 'asset-code-input'));
    },

    // Checks whether `code` is itself the RFID tag of a real, resolvable
    // asset - not just its barcode or asset ID - so addItem() can decide
    // whether a single scan already proves physical possession. Best-effort
    // only: a network hiccup, no match, or a match via barcode/asset ID
    // instead of rfid_id all just return false, which simply falls through
    // to the normal two-scan flow - this never blocks adding the item, it
    // only ever skips a redundant prompt.
    async _looksLikeRfidScan(code) {
      let asset;
      try {
        const res = await fetch(`/api/v1/assets/barcode/${encodeURIComponent(code)}`);
        if (!res.ok) return false;
        asset = await res.json();
      } catch (err) {
        return false;
      }
      if (!asset.rfid_id) return false;
      const upper = code.toUpperCase();
      if (asset.rfid_id.toUpperCase() === upper) return true;
      // Some readers set to type scans as keyboard input only type the
      // decimal value of an EPC's last 3 bytes, not the full hex ID - see
      // utils/validators.py's decimal_scan_hex_suffix() docstring for the
      // real example this is derived from.
      if (/^\d+$/.test(code)) {
        const n = parseInt(code, 10);
        if (n <= 0xffffff) {
          const suffix = n.toString(16).toUpperCase().padStart(6, '0');
          if (asset.rfid_id.toUpperCase().endsWith(suffix)) return true;
        }
      }
      return false;
    },

    // Shared by the manual scan input above and the browse-available-assets
    // panel below - both end up adding one asset code to the same cart, so
    // the actual API call + cart-list DOM update only lives once. Returns
    // true/false so each caller can decide what to do with its own control
    // (clear the input vs. re-enable a panel row) on success/failure.
    // rfidCode is optional - see the template comment on the RFID input.
    async _addAssetByCode(clientId, assetCode, rfidCode, quantity) {
      const s = state.get(clientId);
      const errorEl = fieldOf(clientId, 'item-error');
      errorEl.classList.add('hidden');
      if (!assetCode || !s.sessionId) return false;

      // Renders the cart row from whatever response eventually comes back -
      // called immediately below on a live response, or later (possibly
      // after this whole call has already returned false) by offline_queue.js
      // once a queued retry succeeds, if this tab is still open. Safe to
      // retry: add-to-cart is guarded server-side by both the atomic
      // status='available'->'reserved' claim and checkout_session_items'
      // UNIQUE(checkout_session_id, asset_id), so a duplicate POST from a
      // retried queue item just comes back 409 already_in_cart, not a
      // second row - see offline_queue.js's module doc.
      const renderAddedItem = (data) => {
        const list = fieldOf(clientId, 'cart-list');
        if (!list) return;
        const isConsumable = data.asset_type === 'consumable';
        const existing = list.querySelector(`li[data-item-id="${data.item_id}"]`);
        if (existing) {
          // Re-scan of an already-in-cart consumable: server already
          // incremented its quantity (add_item_to_checkout_session) - just
          // reflect the new total, don't add a second row. A retried/queued
          // duplicate of the SAME add lands here too and is a harmless no-op
          // (same quantity value written again).
          const qtyEl = existing.querySelector('[data-cart-qty]');
          if (qtyEl) qtyEl.textContent = `x${data.quantity}`;
          flashHighlight(existing, 2000);
          document.body.dispatchEvent(new Event('assets-refreshed', { bubbles: true }));
          return;
        }
        const li = document.createElement('li');
        // flex-col (not the plain row this used to be) whenever a
        // reservation notice needs a second line underneath - same
        // structure either way otherwise, so a plain item's row still
        // looks identical to before.
        li.className = data.reservation_notice
          ? 'flex flex-col gap-1 px-3 py-1.5 rounded-lg bg-base-200 text-sm'
          : 'flex items-center justify-between gap-2 px-3 py-1.5 rounded-lg bg-base-200 text-sm';
        li.dataset.itemId = data.item_id;

        const row = data.reservation_notice ? document.createElement('div') : li;
        if (data.reservation_notice) row.className = 'flex items-center justify-between gap-2';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'truncate flex-1';
        nameSpan.textContent = data.barcode_id ? `${data.asset_name} · ${data.barcode_id}` : data.asset_name;
        row.appendChild(nameSpan);

        if (isConsumable) {
          // Quantity stepper instead of a single remove icon - "-" decrements
          // the cart line (removing it outright once it hits zero), "+"
          // re-runs the same add-to-cart call that a rescan would trigger.
          const stepper = document.createElement('div');
          stepper.className = 'flex items-center gap-1.5 shrink-0';
          const minusBtn = document.createElement('button');
          minusBtn.type = 'button';
          minusBtn.className = 'btn btn-ghost btn-xs btn-circle';
          minusBtn.setAttribute('aria-label', `Remove one ${data.asset_name}`);
          minusBtn.textContent = '-';
          minusBtn.onclick = () => Checkout.removeItem(minusBtn, data.item_id, li, 1);
          const qtySpan = document.createElement('span');
          qtySpan.className = 'text-xs font-medium w-8 text-center';
          qtySpan.dataset.cartQty = '';
          qtySpan.textContent = `x${data.quantity}`;
          const plusBtn = document.createElement('button');
          plusBtn.type = 'button';
          plusBtn.className = 'btn btn-ghost btn-xs btn-circle';
          plusBtn.setAttribute('aria-label', `Add one more ${data.asset_name}`);
          plusBtn.textContent = '+';
          plusBtn.onclick = () => this._addAssetByCode(clientId, assetCode, null, 1);
          stepper.appendChild(minusBtn);
          stepper.appendChild(qtySpan);
          stepper.appendChild(plusBtn);
          li.appendChild(stepper);
        } else {
          const removeBtn = document.createElement('button');
          removeBtn.type = 'button';
          removeBtn.className = 'text-error hover:text-error/70 shrink-0';
          removeBtn.setAttribute('aria-label', `Remove ${data.asset_name}`);
          removeBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>';
          removeBtn.onclick = () => Checkout.removeItem(removeBtn, data.item_id, li);
          row.appendChild(removeBtn);
        }
        if (row !== li) li.appendChild(row);

        // Advisory-only heads-up (see services/transaction_service.py's
        // add_item_to_checkout_session) - this asset has an upcoming
        // reservation, but nothing here blocks the checkout itself, just
        // surfaces it the same moment staff scan it in, same as the
        // "RESERVED FOR" banner on the asset's own Details panel.
        if (data.reservation_notice) {
          // Built with textContent, not an innerHTML template string - this
          // file is shared across every page (including ones with no local
          // escapeHtml helper of their own), and member_name ultimately
          // comes from user-entered data.
          const notice = document.createElement('p');
          notice.className = 'text-xs text-warning flex items-start gap-1';
          notice.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" class="w-3 h-3 mt-0.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>';
          const noticeText = document.createElement('span');
          noticeText.textContent = `Reserved for ${data.reservation_notice.member_name}, ${formatDateTime12h(data.reservation_notice.start_at)} – ${formatDateTime12h(data.reservation_notice.end_at)}`;
          notice.appendChild(noticeText);
          li.appendChild(notice);
        }

        list.appendChild(li);
        // Confirms the scan actually landed - same green glow already used
        // for an already-in-cart rescan, just on the freshly-added row this
        // time. Matters most right after an RFID verification tap (the whole
        // point of that second scan is "yes, this is the real tool"), but
        // it's shown for a plain barcode-only add too so the feedback is
        // consistent either way.
        flashHighlight(li, 2000);
        // This asset just flipped available -> reserved (or a consumable's
        // quantity dropped) - the Inventory table (if it's the page
        // underneath, or wherever staff navigate to next) shouldn't need a
        // manual refresh to notice, same as every other status-changing
        // action in the app already does via this same event.
        document.body.dispatchEvent(new Event('assets-refreshed', { bubbles: true }));

        // Remembers which asset code produced which cart item id, so the
        // browse panel (if open) can grey an asset out the moment it's
        // added, and un-grey it again if the item is later removed --
        // without this we'd only know the asset's *name*, not the code
        // the panel keys its rows on.
        if (!s.cartAssetCodes) s.cartAssetCodes = new Map();
        s.cartAssetCodes.set(String(data.item_id), assetCode);
        this._updateCartCount(clientId);
      };

      try {
        const res = await (window.OfflineQueue ? window.OfflineQueue.queuedFetch : fetch)(
          `/api/v1/checkout-sessions/${s.sessionId}/items/add`,
          {
            method: 'POST',
            headers: csrfHeaders(),
            body: JSON.stringify({ asset_code: assetCode, rfid_code: rfidCode || null, quantity: quantity || 1 }),
          },
          { label: `adding ${assetCode}`, onSuccess: async (queuedRes) => {
            if (queuedRes.ok) renderAddedItem(await queuedRes.json());
          } }
        );
        const data = await res.json();

        if (!res.ok) {
          if (res.status === 401) { showSessionTimeoutModal(); return false; }
          errorEl.textContent = data.error || "Couldn't add that asset.";
          errorEl.classList.remove('hidden');
          if (data.code === 'already_in_cart') flashCartItem(clientId, data.item_id);
          return false;
        }

        renderAddedItem(data);
        return true;
      } catch (err) {
        if (window.OfflineQueue && window.OfflineQueue.isOfflineQueuedError(err)) {
          // Already toasted "saved offline" by queuedFetch - nothing more to
          // show here; the row appears once the retry succeeds (onSuccess
          // above), or silently server-side if this tab gets reloaded first.
          return false;
        }
        errorEl.textContent = 'Network error - check your connection and try again.';
        errorEl.classList.remove('hidden');
        return false;
      }
    },

    // decrementBy: consumable-only ("-" stepper button removes just one
    // unit); omitted/undefined removes the whole cart line, as before.
    async removeItem(el, itemId, li, decrementBy) {
      const clientId = clientIdOf(el);
      const s = state.get(clientId);
      if (!s || !s.sessionId) return;
      try {
        const url = `/api/v1/checkout-sessions/${s.sessionId}/items/${itemId}/remove`
          + (decrementBy ? `?decrement_by=${encodeURIComponent(decrementBy)}` : '');
        const res = await fetch(url, { method: 'POST', headers: csrfHeaders() });
        const data = await res.json().catch(() => ({}));

        if (decrementBy && data && data.removed === false) {
          // Line still has units left - just update the displayed count,
          // the row itself stays. Still a real quantity change on a
          // consumable (Inventory shows "Qty: N" live), so still worth
          // refreshing the table underneath.
          const qtyEl = li.querySelector('[data-cart-qty]');
          if (qtyEl) qtyEl.textContent = `x${data.quantity}`;
          document.body.dispatchEvent(new Event('assets-refreshed', { bubbles: true }));
          return;
        }

        li.remove();
        this._updateCartCount(clientId);
        // Removing releases this asset back to 'available' (or restores a
        // consumable's quantity) - same reasoning as complete()/_performCancel().
        document.body.dispatchEvent(new Event('assets-refreshed', { bubbles: true }));

        const assetCode = s.cartAssetCodes && s.cartAssetCodes.get(String(itemId));
        if (assetCode) {
          s.cartAssetCodes.delete(String(itemId));
          const row = fieldOf(clientId, 'available-assets-list')
            ?.querySelector(`button[data-asset-code="${CSS.escape(assetCode)}"]`);
          if (row) row.disabled = false;
        }
      } catch (err) {
        showToast("Couldn't remove that item - try again.", 'error');
      }
    },

    // Toggles the flyout that lists everything currently available to
    // borrow, so staff can browse/pick instead of only scanning barcodes
    // one at a time. Fetches fresh on every open rather than caching --
    // availability changes as other checkouts happen.
    toggleAvailableAssets(el) {
      const clientId = clientIdOf(el);
      const panel = fieldOf(clientId, 'available-assets-panel');
      if (!panel) return;
      const opening = panel.classList.contains('hidden');
      if (opening) this._positionFlyoutPanel(clientId, 'available-assets-panel');
      panel.classList.toggle('hidden');
      if (opening) {
        fieldOf(clientId, 'available-assets-filter').value = '';
        this._loadAvailableAssets(clientId);
      }
    },

    // The panel is `position: fixed` with no CSS anchor of its own (see
    // the markup comment for why it can't just be `absolute` inside
    // .modal-box), so this computes where "flush against the panel's left
    // edge" actually is from .modal-box's live bounding rect each time
    // it's opened, and again on resize while it's open (below) - the
    // side panel's rendered width is viewport-dependent under the 26rem
    // breakpoint (max-width: 92vw in base.html). Shared by both the
    // available-assets and available-members flyouts - same geometry
    // either way, just a different panel role.
    _positionFlyoutPanel(clientId, panelRole) {
      const box = dialogOf(clientId)?.querySelector('.modal-box');
      const panel = fieldOf(clientId, panelRole);
      if (!box || !panel) return;
      const rect = box.getBoundingClientRect();
      // Top and height mirror .modal-box's own rect exactly (rather than
      // inset + maxHeight-24) so the flyout's top and bottom edges line up
      // flush with the panel it's attached to - same height, not just
      // capped at it. `height` (not maxHeight) is what's needed here: the
      // panel's flex-1 list only grows to fill a *definite* parent height,
      // so a maxHeight alone left it sized to content instead of matching
      // the modal.
      panel.style.top = rect.top + 'px';
      panel.style.right = (window.innerWidth - rect.left + 12) + 'px';
      panel.style.height = rect.height + 'px';
    },

    async _loadAvailableAssets(clientId) {
      const s = state.get(clientId);
      const listEl = fieldOf(clientId, 'available-assets-list');
      const loadingEl = fieldOf(clientId, 'available-assets-loading');
      const errorEl = fieldOf(clientId, 'available-assets-error');
      listEl.innerHTML = '';
      errorEl.classList.add('hidden');
      loadingEl.classList.remove('hidden');

      try {
        // GET /checkout-sessions/available-assets (routes/transactions.py)
        // returns { assets: [{ code, name, category }, ...] }. `code` is
        // each asset's barcode_id - sent back through items/add below so
        // a clicked row resolves to the same asset that was shown.
        const res = await fetch('/api/v1/checkout-sessions/available-assets', { headers: csrfHeaders() });
        const data = await res.json();

        if (!res.ok) {
          if (res.status === 401) { showSessionTimeoutModal(); return; }
          errorEl.textContent = data.error || "Couldn't load available assets.";
          errorEl.classList.remove('hidden');
          return;
        }

        const addedCodes = s.cartAssetCodes ? new Set(s.cartAssetCodes.values()) : new Set();
        const assets = data.assets || [];
        assets.forEach((asset) => listEl.appendChild(this._buildAvailableAssetRow(clientId, asset, addedCodes.has(asset.code))));

        if (assets.length === 0) {
          const empty = document.createElement('li');
          empty.className = 'text-xs text-base-content/50 text-center py-4';
          empty.textContent = 'No available assets.';
          listEl.appendChild(empty);
        }
      } catch (err) {
        errorEl.textContent = 'Network error - check your connection and try again.';
        errorEl.classList.remove('hidden');
      } finally {
        loadingEl.classList.add('hidden');
      }
    },

    _buildAvailableAssetRow(clientId, asset, alreadyAdded) {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'w-full text-left px-2 py-1.5 rounded-lg hover:bg-base-200 disabled:opacity-40 disabled:hover:bg-transparent';
      btn.dataset.assetCode = asset.code;
      btn.disabled = !!alreadyAdded;
      btn.onclick = () => Checkout.selectAvailableAsset(btn, clientId, asset.code);

      const nameSpan = document.createElement('span');
      nameSpan.className = 'block text-sm truncate';
      nameSpan.textContent = asset.name;
      btn.appendChild(nameSpan);

      // `code` is the asset's barcode_id (see _loadAvailableAssets above) --
      // shown so staff can visually cross-check a row against a physical
      // barcode instead of only matching by name.
      const codeSpan = document.createElement('span');
      codeSpan.className = 'block text-xs font-mono text-base-content/40 truncate';
      codeSpan.textContent = asset.code;
      btn.appendChild(codeSpan);

      if (asset.category) {
        const categorySpan = document.createElement('span');
        categorySpan.className = 'block text-xs text-base-content/50 truncate';
        categorySpan.textContent = asset.category;
        btn.appendChild(categorySpan);
      }

      li.appendChild(btn);
      return li;
    },

    // Client-side filter over whatever's already loaded - no re-fetch,
    // just show/hide rows. Fine at inventory list sizes; swap for a
    // debounced server query if the real list ends up huge.
    filterAvailableAssets(input) {
      const clientId = clientIdOf(input);
      const q = input.value.trim().toLowerCase();
      fieldOf(clientId, 'available-assets-list').querySelectorAll('button[data-asset-code]').forEach((btn) => {
        btn.closest('li').classList.toggle('hidden', !btn.textContent.toLowerCase().includes(q));
      });
    },

    // Clicking a row in the panel adds it exactly like scanning its
    // barcode would - same endpoint, same cart-list DOM update - it just
    // disables the row afterward instead of clearing a text input.
    async selectAvailableAsset(btn, clientId, assetCode) {
      // Picking from the browse panel is its own one-shot add, unrelated to
      // whatever might be mid-verification in the scan field - clear that
      // so a leftover pending code from a different tool doesn't get
      // silently attached as this item's "RFID verification" on the next
      // Enter in that field.
      const s = state.get(clientId);
      s.pendingAssetCode = null;
      fieldOf(clientId, 'pending-verify-hint')?.classList.add('hidden');

      btn.disabled = true;
      const added = await this._addAssetByCode(clientId, assetCode);
      if (!added) btn.disabled = false;
    },

    // Step 1's "Show available members" flyout - same open/load/filter/select
    // shape as the available-assets one above, just listing eligible
    // (active, unrestricted) members instead of borrowable assets.
    toggleAvailableMembers(el) {
      const clientId = clientIdOf(el);
      const panel = fieldOf(clientId, 'available-members-panel');
      if (!panel) return;
      const opening = panel.classList.contains('hidden');
      if (opening) this._positionFlyoutPanel(clientId, 'available-members-panel');
      panel.classList.toggle('hidden');
      if (opening) {
        fieldOf(clientId, 'available-members-filter').value = '';
        this._loadAvailableMembers(clientId);
      }
    },

    async _loadAvailableMembers(clientId) {
      const listEl = fieldOf(clientId, 'available-members-list');
      const loadingEl = fieldOf(clientId, 'available-members-loading');
      const errorEl = fieldOf(clientId, 'available-members-error');
      listEl.innerHTML = '';
      errorEl.classList.add('hidden');
      loadingEl.classList.remove('hidden');

      try {
        // GET /checkout-sessions/available-members (routes/transactions.py)
        // returns { members: [{ code, name, department }, ...] }. `code` is
        // each member's barcode_id - fed straight into startSession below.
        const res = await fetch('/api/v1/checkout-sessions/available-members', { headers: csrfHeaders() });
        const data = await res.json();

        if (!res.ok) {
          if (res.status === 401) { showSessionTimeoutModal(); return; }
          errorEl.textContent = data.error || "Couldn't load available members.";
          errorEl.classList.remove('hidden');
          return;
        }

        const members = data.members || [];
        members.forEach((member) => listEl.appendChild(this._buildAvailableMemberRow(clientId, member)));

        if (members.length === 0) {
          const empty = document.createElement('li');
          empty.className = 'text-xs text-base-content/50 text-center py-4';
          empty.textContent = 'No available members.';
          listEl.appendChild(empty);
        }
      } catch (err) {
        errorEl.textContent = 'Network error - check your connection and try again.';
        errorEl.classList.remove('hidden');
      } finally {
        loadingEl.classList.add('hidden');
      }
    },

    // Restricted members are shown, not hidden - staff should still see the
    // whole roster (a member who's just vanished from the list looks like a
    // data bug), they just can't be picked: the row is disabled and flagged
    // with why, matching the same restriction start_checkout_session would
    // otherwise reject the barcode for anyway.
    _buildAvailableMemberRow(clientId, member) {
      const li = document.createElement('li');
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'w-full text-left px-2 py-1.5 rounded-lg hover:bg-base-200 disabled:opacity-50 disabled:hover:bg-transparent disabled:cursor-not-allowed';
      btn.dataset.memberCode = member.code;
      if (member.restricted) {
        btn.disabled = true;
        btn.title = member.restriction_reason || 'This member is restricted';
      } else {
        btn.onclick = () => Checkout.selectAvailableMember(btn, clientId, member.code);
      }

      const nameRow = document.createElement('span');
      nameRow.className = 'flex items-center gap-1.5';
      const nameSpan = document.createElement('span');
      nameSpan.className = 'text-sm truncate';
      nameSpan.textContent = member.name;
      nameRow.appendChild(nameSpan);
      if (member.restricted) {
        const badge = document.createElement('span');
        badge.className = 'badge badge-error badge-xs shrink-0';
        badge.textContent = 'Restricted';
        nameRow.appendChild(badge);
      }
      btn.appendChild(nameRow);

      const codeSpan = document.createElement('span');
      codeSpan.className = 'block text-xs font-mono text-base-content/40 truncate';
      codeSpan.textContent = member.code;
      btn.appendChild(codeSpan);

      if (member.department) {
        const deptSpan = document.createElement('span');
        deptSpan.className = 'block text-xs text-base-content/50 truncate';
        deptSpan.textContent = member.department;
        btn.appendChild(deptSpan);
      }

      li.appendChild(btn);
      return li;
    },

    filterAvailableMembers(input) {
      const clientId = clientIdOf(input);
      const q = input.value.trim().toLowerCase();
      fieldOf(clientId, 'available-members-list').querySelectorAll('button[data-member-code]').forEach((btn) => {
        btn.closest('li').classList.toggle('hidden', !btn.textContent.toLowerCase().includes(q));
      });
    },

    // Picking a row starts the session exactly as if that barcode had been
    // scanned/typed into the member-barcode field and Start clicked.
    selectAvailableMember(btn, clientId, memberCode) {
      const input = fieldOf(clientId, 'member-barcode-input');
      input.value = memberCode;
      fieldOf(clientId, 'available-members-panel')?.classList.add('hidden');
      this.startSession(input);
    },

    _updateCartCount(clientId) {
      const count = cartItemCount(clientId);
      fieldOf(clientId, 'cart-count').textContent = `${count} item${count === 1 ? '' : 's'}`;
      if (isMinimized(clientId)) updateDockLabel(clientId);
    },

    async complete(btn) {
      const clientId = clientIdOf(btn);
      const s = state.get(clientId);
      const errorEl = fieldOf(clientId, 'confirm-error');
      errorEl.classList.add('hidden');
      if (!s || !s.sessionId) return;

      const dueDateInput = fieldOf(clientId, 'due-date-input').value;
      const dueDate = dueDateInput ? dueDateInput.replace('T', ' ') + ':00' : null;

      try {
        const res = await fetch(`/api/v1/checkout-sessions/${s.sessionId}/checkout`, {
          method: 'POST',
          headers: csrfHeaders(),
          body: JSON.stringify({ due_date: dueDate }),
        });
        const data = await res.json();

        if (!res.ok) {
          if (res.status === 401) { showSessionTimeoutModal(); return; }
          errorEl.textContent = data.error || "Couldn't complete checkout.";
          errorEl.classList.remove('hidden');
          return;
        }

        s.sessionId = null;
        s.completedCount += 1;
        showToast(data.message || 'Checkout complete', 'success');
        document.body.dispatchEvent(new Event('transactions-refreshed', { bubbles: true }));
        // Every scanned asset just flipped reserved -> borrowed - the
        // Inventory table (if it's the page underneath, or the next page
        // visited) should already show that instead of needing a manual
        // refresh, same as every other status-changing action here.
        document.body.dispatchEvent(new Event('assets-refreshed', { bubbles: true }));
        this._resetForNextTrainee(clientId);
      } catch (err) {
        errorEl.textContent = 'Network error - check your connection and try again.';
        errorEl.classList.remove('hidden');
      }
    },

    // Called after each successful checkout instead of closing the modal.
    // Keeps this instance's dialog open, resets it to step 1, and updates
    // its running tally so staff can scan the next trainee's barcode
    // immediately without reopening "New Checkout" from scratch.
    _resetForNextTrainee(clientId) {
      if (isMinimized(clientId)) this.expandById(clientId);

      const s = state.get(clientId);
      s.memberFirstName = null;
      s.memberBarcode = null;
      s.pendingAssetCode = null;
      fieldOf(clientId, 'pending-verify-hint')?.classList.add('hidden');
      fieldOf(clientId, 'cart-list').innerHTML = '';
      fieldOf(clientId, 'cart-count').textContent = '0 items';
      fieldOf(clientId, 'member-barcode-label').textContent = '';
      fieldOf(clientId, 'due-date-input').value = '';
      fieldOf(clientId, 'member-barcode-input').value = '';

      fieldOf(clientId, 'step-cart').classList.add('hidden');
      fieldOf(clientId, 'complete-btn').classList.add('hidden');
      fieldOf(clientId, 'minimize-btn').classList.add('hidden');
      fieldOf(clientId, 'step-member').classList.remove('hidden');

      fieldOf(clientId, 'step-hint').textContent =
        'Scan the next trainee\u2019s barcode to continue.';
      const tally = fieldOf(clientId, 'group-tally');
      tally.textContent = `${s.completedCount} borrower${s.completedCount === 1 ? '' : 's'} checked out this session`;
      tally.classList.remove('hidden');
      fieldOf(clientId, 'cancel-btn').textContent = 'Finish';

      fieldOf(clientId, 'member-barcode-input').focus();
    },

    // Actually tears down one instance's session and removes its DOM.
    // Only ever called once the user has confirmed (or there was nothing
    // to confirm). Only affects this instance - any other open or
    // minimized checkout is untouched.
    async _performCancel(clientId) {
      const s = state.get(clientId);
      if (s && s.sessionId) {
        try {
          await fetch(`/api/v1/checkout-sessions/${s.sessionId}/cancel`, {
            method: 'POST',
            headers: csrfHeaders(),
          });
          // Cancelling releases every scanned asset back to 'available' -
          // same "the table underneath shouldn't need a manual refresh to
          // notice" reasoning as every other status-changing action here.
          document.body.dispatchEvent(new Event('assets-refreshed', { bubbles: true }));
        } catch (err) {
          // Best-effort - the session will also auto-cancel on its own
          // after SESSION_STALE_MINUTES if this fails.
        }
      }
      dialogOf(clientId)?.remove();
      dockOf(clientId)?.remove();
      state.delete(clientId);
      syncPanelOpenClass();
    },

    // Pure restyle - the dialog, its cart DOM, and its state entry are
    // all untouched. Only visibility toggles, and only for this instance.
    minimize(el) {
      const clientId = clientIdOf(el);
      updateDockLabel(clientId);
      const dialog = dialogOf(clientId);
      // close() (not just hiding .modal-box/.modal-backdrop) is what
      // actually takes the dialog out of the top layer - otherwise the
      // native ::backdrop stays rendered and the rest of the page stays
      // inert/unclickable even though our own elements are visually hidden.
      if (dialog.open) dialog.close();
      dockOf(clientId).classList.remove('hidden');
      // The available-assets panel is `position: fixed`, so closing the
      // dialog doesn't visually hide it the way it hides everything still
      // nested inside .modal-box - without this it would just sit there
      // floating over the page, and would reappear pre-opened (with a now
      // stale list) the next time this instance expands.
      fieldOf(clientId, 'available-assets-panel')?.classList.add('hidden');
      fieldOf(clientId, 'available-members-panel')?.classList.add('hidden');
      syncPanelOpenClass();
    },

    // Expanding one instance minimizes whatever else is currently
    // expanded first - two full-size native <dialog>s open at once is
    // confusing, so at most one is ever shown expanded even though
    // several can sit minimized in the tray at the same time.
    expand(el) {
      this.expandById(clientIdOf(el));
    },

    expandById(clientId) {
      document.querySelectorAll('#checkout-modal-container > dialog.checkout-modal[open]').forEach((d) => {
        if (d.id !== 'checkout-modal-' + clientId) this.minimize(d.querySelector('[data-role="minimize-btn"]'));
      });
      const dialog = dialogOf(clientId);
      if (!dialog.open) dialog.showModal();
      dockOf(clientId).classList.add('hidden');
      syncPanelOpenClass();
    },

    // Entry point for every dismiss path on one instance: its Cancel
    // button, backdrop click, Escape (via the data-close-guard hook
    // below), and its dock's close button.
    //
    // Closing from the dock ALWAYS asks for confirmation first, whether
    // or not anything's been scanned - a lightweight native confirm()
    // rather than expanding the full dialog, so the dock doesn't flash
    // open just to close again.
    //
    // Closing from an already-open dialog (Cancel, backdrop, Escape)
    // keeps the lower-friction behavior: an empty cart closes right
    // away since there's nothing to lose and the user can already see
    // that; a cart with items shows the full discard-confirmation panel
    // in place, since the dialog's already open and there's real
    // scanned progress on the line.
    requestClose(el) {
      const clientId = clientIdOf(el);
      const count = cartItemCount(clientId);

      if (isMinimized(clientId)) {
        const s = state.get(clientId);
        const who = (s && s.memberFirstName) ? `${s.memberFirstName}'s checkout` : 'this checkout';
        const message = count > 0
          ? `Close ${who}? ${count} scanned item${count === 1 ? '' : 's'} will be lost.`
          : `Close ${who}?`;
        pendingCloseClientId = clientId;
        document.getElementById('checkout-close-confirm-message').textContent = message;
        document.getElementById('checkout-close-confirm-modal').showModal();
        return;
      }

      if (count === 0) {
        this._performCancel(clientId);
        return;
      }

      fieldOf(clientId, 'discard-count').textContent = `${count} scanned item${count === 1 ? '' : 's'}`;
      fieldOf(clientId, 'available-assets-panel')?.classList.add('hidden');
      fieldOf(clientId, 'available-members-panel')?.classList.add('hidden');
      fieldOf(clientId, 'step-member').classList.add('hidden');
      fieldOf(clientId, 'step-cart').classList.add('hidden');
      fieldOf(clientId, 'step-discard').classList.remove('hidden');
      fieldOf(clientId, 'main-actions').classList.add('hidden');
      fieldOf(clientId, 'discard-actions').classList.remove('hidden');
    },

    // "Close checkout" in the shared confirm dialog - tears down
    // whichever instance requestClose() flagged as pending. Reads
    // pendingCloseClientId into a local first: dialog.close() fires its
    // 'close' event synchronously, and that event also clears
    // pendingCloseClientId (see the listener below), so reading it after
    // calling close() would always see null.
    confirmCloseFromDock() {
      const clientId = pendingCloseClientId;
      document.getElementById('checkout-close-confirm-modal').close();
      if (clientId) this._performCancel(clientId);
    },

    // "Keep it" (or backdrop/Escape) - just dismiss, instance untouched.
    cancelCloseConfirm() {
      document.getElementById('checkout-close-confirm-modal').close();
      pendingCloseClientId = null;
    },

    cancelDiscard(el) {
      const clientId = clientIdOf(el);
      const s = state.get(clientId);
      fieldOf(clientId, 'step-discard').classList.add('hidden');
      fieldOf(clientId, 'discard-actions').classList.add('hidden');
      fieldOf(clientId, 'main-actions').classList.remove('hidden');
      // Return to whichever step the user was actually on.
      if (s && s.sessionId) {
        fieldOf(clientId, 'step-cart').classList.remove('hidden');
        fieldOf(clientId, 'asset-code-input').focus();
      } else {
        fieldOf(clientId, 'step-member').classList.remove('hidden');
        fieldOf(clientId, 'member-barcode-input').focus();
      }
    },

    confirmDiscard(el) {
      this._performCancel(clientIdOf(el));
    },
  };
})();

// Keeps any currently-open available-assets/available-members panel(s)
// flush against their checkout dialog's edge as the viewport resizes --
// needed because the side panel's own width is responsive (max-width:
// 92vw) below the 26rem breakpoint, so a stale position computed at open
// time would drift out of alignment.
window.addEventListener('resize', function () {
  ['available-assets-panel', 'available-members-panel'].forEach((role) => {
    document.querySelectorAll(`[data-role="${role}"]:not(.hidden)`).forEach((panel) => {
      const clientId = panel.closest('[data-client-id]')?.dataset.clientId;
      if (clientId) Checkout._positionFlyoutPanel(clientId, role);
    });
  });
});

// data-close-guard hook for the shared Escape-key handler below. That
// handler only knows the topmost *open* dialog exists, it doesn't pass it
// in - but since Checkout.expandById() never lets more than one checkout
// dialog be open at a time, there's at most one to find here.
window.requestCheckoutModalClose = function () {
  const minimizeBtn = document.querySelector('#checkout-modal-container > dialog.checkout-modal[open] [data-role="minimize-btn"]');
  if (minimizeBtn) Checkout.requestClose(minimizeBtn);
};

// Shared data-close-guard target for the CSV import modals (Assets/
// Members/Staff - templates/{assets,members,staff}/_import_modal.html),
// each set data-close-guard="guardImportModalClose" on their outer
// .modal.modal-open div. Only one of these can ever be open at a time
// (same one-modal-in-#modal-container assumption requestCheckoutModalClose
// above makes), so this doesn't need to know which app it's for. The
// import form itself toggles data-uploading="1" on this same div via
// hx-on::before-request/after-request (there's no htmx-request class on
// the outer div itself to check instead - that class only lands on the
// elements htmx actually targets/indicates). While a preview/commit
// request is in flight, the click/Escape is swallowed entirely rather
// than closing mid-upload and losing the file selection or leaving an
// import half-applied with no visible outcome.
window.guardImportModalClose = function () {
  const modalEl = document.querySelector('.modal.modal-open[data-close-guard="guardImportModalClose"]');
  if (!modalEl || modalEl.dataset.uploading === '1') return;
  modalEl.remove();
};

// Native Escape-to-close on <dialog> bypasses our explicit "Keep it"
// button, so this catches every way the shared close-confirm dialog can
// end up closed (button clicks, backdrop click, Escape) and makes sure
// pendingCloseClientId never lingers stale.
document.getElementById('checkout-close-confirm-modal')?.addEventListener('close', function () {
  Checkout.cancelCloseConfirm();
});

// "?" opens Keyboard shortcuts from anywhere - skipped while typing in a
// field (an input/textarea/contenteditable, or anything already inside an
// open modal) so it doesn't hijack a literal "?" character someone's
// typing into a search box or form field. Mirrors the "?" convention used
// elsewhere (GitHub, Slack, etc.) for "show me the shortcuts" - opens
// #keyboard-shortcuts-modal specifically, not the general Help modal
// (see the account-menu rows above, now two separate entries).
document.addEventListener('keydown', function (e) {
  if (e.key !== '?') return;

  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  if (e.target.closest('dialog[open]')) return;

  e.preventDefault();
  document.getElementById('keyboard-shortcuts-modal')?.showModal();
});

document.addEventListener('keydown', function (e) {
  if (e.key !== 'Escape') return;

  const folderBrowser = document.getElementById('export-folder-browser');
  if (folderBrowser && !folderBrowser.classList.contains('hidden')) return;

  const containers = [
    document.getElementById('modal-container'),
    document.getElementById('checkout-modal-container'),
    // Notification settings (opened from the System page and the
    // notification-bell dropdown's gear icon) targets this container
    // specifically, not #modal-container - left out of this list
    // before, so Escape silently did nothing while it was open.
    document.getElementById('global-modal-container'),
  ].filter(Boolean);
  // Three modal patterns coexist in this app: native <dialog> (opened via
  // showModal(), e.g. Manage Categories/Departments - those two also
  // listen for the dialog's own native Escape-triggered 'close' event
  // themselves and remove() on it, so this handler finding and
  // remove()-ing them too is redundant but harmless), the older
  // div.modal.modal-open pattern (no dialog semantics, so no native
  // Escape handling at all) that most of the htmx-swapped-in Add/Edit/
  // Delete/Import/Export forms still use, and the .fixed.inset-0.z-50
  // "sheet" pattern used by the Details/Profile slide-in panels (assets,
  // members, staff - each used to register its own document-level
  // escClose listener that unconditionally grabbed the FIRST such element
  // in the whole document via querySelector, so opening e.g. Edit
  // *from* Details - appended as a sibling .modal.modal-open, both
  // direct children of #modal-container - meant one Escape press fired
  // both that stale listener and this one, closing the sheet underneath
  // as well as the modal on top. Folding all three patterns into this
  // single container-scoped, topmost-only handler and deleting the
  // per-template ones (see assets/_details_modal.html,
  // members/_profile_modal.html, staff/_profile_modal.html) fixes that -
  // one Escape now only ever closes the single topmost modal, whatever
  // pattern it uses.
  const modals = containers.flatMap((c) =>
    Array.from(c.querySelectorAll(':scope > dialog[open], :scope > .modal.modal-open, :scope > .fixed.inset-0.z-50'))
  );
  if (modals.length === 0) return;

  const topModal = modals[modals.length - 1];

  // Modals that hold unsaved/in-progress state (e.g. an open checkout
  // session with scanned items) can opt in with data-close-guard="fnName"
  // to intercept Escape and decide for themselves whether it's safe to
  // close, rather than being force-removed from under them.
  const guardFnName = topModal.dataset.closeGuard;
  if (guardFnName && typeof window[guardFnName] === 'function') {
    window[guardFnName]();
    return;
  }

  topModal.remove();
});

// Backdrop-click-to-close for the older div.modal.modal-open pattern (see
// the Escape handler's comment above for why two modal patterns coexist).
// Native <dialog> modals already get this for free via their own
// <form method="dialog" class="modal-backdrop"> sibling - clicking the
// backdrop submits that hidden form, which calls the dialog's native
// close(), no JS needed. The div-based pattern has no dialog semantics
// at all, so it's never had backdrop-click-close - this adds it,
// sharing the exact same data-close-guard contract the Escape handler
// above uses, so a modal only ever needs to opt into ONE guard function
// to protect itself from both dismissal paths at once.
document.addEventListener('click', function (e) {
  // Only fires when the click landed directly on the outer wrapper
  // itself (the backdrop) - not on .modal-box or anything inside it.
  // e.target === the modal div is the standard way to detect that with
  // a single delegated listener instead of attaching one per modal.
  if (!e.target.classList || !e.target.classList.contains('modal') || !e.target.classList.contains('modal-open')) {
    return;
  }

  const guardFnName = e.target.dataset.closeGuard;
  if (guardFnName && typeof window[guardFnName] === 'function') {
    window[guardFnName]();
    return;
  }

  e.target.remove();
});

(function () {
  const pending = sessionStorage.getItem('pending-toast');
  if (pending) {
    sessionStorage.removeItem('pending-toast');
    try {
      const { message, type } = JSON.parse(pending);
      showToast(message, type);
    } catch (e) {}
  }
})();

// # # # GLOBAL SEARCH -------------------------------------------------------
// Content header's Ctrl/Cmd+/ search bar (base.html) - #global-search-results'
// own visibility is driven purely by whether it's :empty (see that element's
// comment in base.html), so this only needs to handle clearing/closing it and
// basic keyboard navigation between the rendered results.

function closeGlobalSearch() {
  const input = document.getElementById('global-search-input');
  const results = document.getElementById('global-search-results');
  if (input) { input.value = ''; input.blur(); }
  if (results) results.innerHTML = '';
}

// Replays the dropdown's fade/slide-in (base.html's .global-search-dropdown-in
// keyframe) every time htmx swaps fresh results into it - removing then
// re-adding the class (with a forced reflow between, via offsetWidth) is
// needed because the element itself never leaves the DOM between searches,
// so just re-adding an already-present class wouldn't restart the animation.
document.body.addEventListener('htmx:afterSwap', function (e) {
  const results = e.target;
  if (!results || results.id !== 'global-search-results') return;
  results.classList.remove('global-search-dropdown-in');
  void results.offsetWidth;
  results.classList.add('global-search-dropdown-in');
});

function handleGlobalSearchKeydown(e) {
  const results = document.getElementById('global-search-results');
  if (!results) return;

  if (e.key === 'Escape') {
    e.preventDefault();
    e.stopPropagation();
    closeGlobalSearch();
    return;
  }

  const items = Array.from(results.querySelectorAll('.global-search-result'));
  if (!items.length) return;

  const current = results.querySelector('.global-search-result-active');
  let index = items.indexOf(current);

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    index = (index + 1) % items.length;
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    index = (index - 1 + items.length) % items.length;
  } else if (e.key === 'Enter') {
    e.preventDefault();
    // Deliberately NOT (current || items[0]).click() here - a real Enter
    // keypress on a type="search" input races against that element's own
    // native search-field behavior (Chrome/Edge can replay the field's prior
    // text right after), which was intermittently swallowing the synthetic
    // click before htmx ever saw it. Driving htmx directly via its public API
    // sidesteps the input entirely, so it can't be re-clobbered afterward.
    const target = current || items[0];
    const hxUrl = target.getAttribute('hx-get');
    // Read the actual hx-target/hx-swap off the element rather than assuming
    // #modal-container/beforeend - true for assets/members/staff results, but
    // Quick Actions' own targets vary (Checkout docks into
    // #checkout-modal-container; Return/Add Member/Add Asset use "innerHTML",
    // not "beforeend" - same as home.html's own Quick Actions buttons).
    const hxTarget = target.getAttribute('hx-target') || '#modal-container';
    const hxSwap = target.getAttribute('hx-swap') || 'beforeend';
    // Pages results are plain <a href> (a full page nav, not an htmx swap -
    // see _global_search_results.html), so they have no hx-get to read here.
    const hrefUrl = target.tagName === 'A' ? target.getAttribute('href') : null;
    closeGlobalSearch();
    if (hxUrl && target.dataset.quickAction === 'checkout') {
      // Same concurrent-checkout cap home.html's own Checkout button enforces
      // via hx-trigger="click[Checkout.countOk()]" - replicated here since
      // htmx.ajax bypasses that trigger guard entirely.
      if (window.Checkout && window.Checkout.countOk()) {
        window.htmx.ajax('GET', hxUrl, { target: hxTarget, swap: hxSwap, values: { client_id: generateClientId() } });
      }
    } else if (hxUrl && window.htmx) {
      window.htmx.ajax('GET', hxUrl, { target: hxTarget, swap: hxSwap });
    } else if (hrefUrl) {
      window.location.href = hrefUrl;
    } else {
      // Account/theme results (search_service.py's _ACCOUNT_ACTIONS) have
      // neither hx-get nor href - just a plain onclick (showModal()/
      // setThemeMode()) baked into the row itself. A real .click() here is
      // fine (unlike the entity-result case above this replaced) since
      // there's no htmx request racing a native search-field side effect -
      // it's a synchronous, self-contained JS call either way. Still works
      // even though closeGlobalSearch() just emptied #global-search-results
      // out from under it - detaching an element doesn't stop a already-bound
      // onclick from firing when .click() is called on it directly.
      target.click();
    }
    return;
  } else {
    return;
  }

  if (current) current.classList.remove('global-search-result-active', 'bg-base-200');
  items[index].classList.add('global-search-result-active', 'bg-base-200');
  items[index].scrollIntoView({ block: 'nearest' });
}

// Click-outside closes the dropdown, same convention as the account/
// notification dropdowns elsewhere in this same header.
document.addEventListener('click', function (e) {
  const wrap = document.getElementById('global-search-wrap');
  if (wrap && !wrap.contains(e.target)) closeGlobalSearch();
});
