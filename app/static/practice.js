/* Progressive enhancement only: every page works without this file; the server validates everything. */
(function () {
  "use strict";

  // Practice page: show how many questions match the chosen filters as they change.
  var picker = document.getElementById("practice-form");
  var matchBox = document.getElementById("match-count");
  if (picker && matchBox) {
    var timer = null;
    var refresh = function () {
      var params = new URLSearchParams();
      new FormData(picker).forEach(function (value, key) {
        if (key !== "count" && value !== "") params.append(key, value);
      });
      fetch("/practice/count?" + params.toString(), { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) { matchBox.textContent = data.error; return; }
          matchBox.textContent = data.count + (data.count === 1 ? " question matches" : " questions match");
        })
        .catch(function () { /* leave the previous text */ });
    };
    picker.addEventListener("change", function () {
      clearTimeout(timer);
      timer = setTimeout(refresh, 150);
    });
  }

  // Question page: keep "Check answer" disabled until an option and a confidence level are both chosen.
  var form = document.getElementById("answer-form");
  var button = document.getElementById("check-btn");
  if (form && button) {
    var ready = function () {
      return form.querySelector('input[name="answer"]:checked') && form.querySelector('input[name="confidence"]:checked');
    };
    var update = function () { button.disabled = !ready(); };
    form.addEventListener("change", update);
    update();
  }
})();
