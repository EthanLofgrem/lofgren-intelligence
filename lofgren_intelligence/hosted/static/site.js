/* Progressive enhancement for the Lofgren Intelligence site and console shell.
   Without this script every tab panel stays visible (tabs are in-page links)
   and every menu stays expanded. No network calls are made. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  function initMenus() {
    var toggles = document.querySelectorAll(".nav-toggle[aria-controls]");
    Array.prototype.forEach.call(toggles, function (button) {
      var target = document.getElementById(button.getAttribute("aria-controls"));
      if (!target) { return; }
      button.addEventListener("click", function () {
        var open = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", open ? "false" : "true");
        target.classList.toggle("is-open", !open);
      });
    });
  }

  function initTabs(list) {
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    if (!tabs.length) { return; }
    var panels = tabs.map(function (tab) {
      return document.getElementById(tab.getAttribute("aria-controls"));
    });

    function select(index, focus) {
      tabs.forEach(function (tab, i) {
        var active = i === index;
        tab.setAttribute("aria-selected", active ? "true" : "false");
        tab.setAttribute("tabindex", active ? "0" : "-1");
        if (panels[i]) { panels[i].hidden = !active; }
      });
      if (focus) { tabs[index].focus(); }
    }

    var initial = 0;
    var hash = window.location.hash.slice(1);
    tabs.forEach(function (tab, i) {
      if (tab.getAttribute("aria-controls") === hash) { initial = i; }
      else if (!hash && tab.getAttribute("aria-selected") === "true") { initial = i; }
    });
    select(initial, false);

    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function (event) {
        event.preventDefault();
        select(i, true);
      });
      tab.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") { next = (i + 1) % tabs.length; }
        else if (event.key === "ArrowLeft" || event.key === "ArrowUp") { next = (i - 1 + tabs.length) % tabs.length; }
        else if (event.key === "Home") { next = 0; }
        else if (event.key === "End") { next = tabs.length - 1; }
        else if (event.key === " ") { next = i; }
        if (next !== null) {
          event.preventDefault();
          select(next, true);
        }
      });
    });
  }

  function init() {
    initMenus();
    Array.prototype.forEach.call(document.querySelectorAll('[role="tablist"]'), initTabs);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
