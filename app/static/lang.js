// Language switch for a question page. Every language is already in the page (see templates/_lang.html); this only changes which one shows.
// It never touches the answer form, the timer or the position, and there is no page load. The choice is saved in the background so the next page uses it.
(function () {
  "use strict";
  var scope = document.querySelector(".lang-scope");
  var forms = document.querySelectorAll(".lang-toggle");
  if (!scope || !forms.length) return;

  var SAID = { en: "Showing English", hi: "Showing Hindi", both: "Showing English and Hindi" };

  var settle = null;
  function show(value) {
    scope.classList.add("lang-switching");                      // lets the newly shown text fade in
    clearTimeout(settle);
    settle = setTimeout(function () { scope.classList.remove("lang-switching"); }, 300);
    scope.setAttribute("data-lang", value);
    forms.forEach(function (form) {
      form.querySelectorAll(".lang-btn").forEach(function (button) {
        var on = button.value === value;
        button.classList.toggle("on", on);
        button.setAttribute("aria-pressed", on ? "true" : "false");
      });
      var live = form.querySelector(".lang-live");
      if (live) live.textContent = SAID[value] || "";          // announced to screen readers
    });
  }

  function save(form, value) {
    try {
      fetch(form.getAttribute("data-save-url"), {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "language=" + encodeURIComponent(value)
      }).catch(function () { /* the display has switched anyway; only the remembered choice is lost */ });
    } catch (e) { /* same */ }
  }

  forms.forEach(function (form) {
    form.addEventListener("click", function (event) {
      var button = event.target.closest(".lang-btn");
      if (!button) return;
      event.preventDefault();                                   // no form post, no page load
      show(button.value);
      save(form, button.value);
    });

    // Left / Right move between the three choices, like any segmented control.
    form.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      var buttons = Array.prototype.slice.call(form.querySelectorAll(".lang-btn"));
      var at = buttons.indexOf(document.activeElement);
      if (at === -1) return;
      event.preventDefault();
      var next = buttons[(at + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
      next.focus();
      next.click();
    });
  });
})();
