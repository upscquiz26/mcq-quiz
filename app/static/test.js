/* Timed-test screen: countdown, autosave and palette updates.
 *
 * The SERVER owns the clock and the answers. This script only displays the countdown (from the deadline the
 * server sent, corrected for any difference between this device's clock and the server's) and sends changes
 * as they happen. If it fails to load, the page still works: each button is a normal form post and the server
 * finishes the test by itself when time is up. */
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  // ---- countdown -------------------------------------------------------------------------------------------
  var bar = document.getElementById("test-bar");
  var timerEl = document.getElementById("timer");
  var finishForm = document.getElementById("auto-finish");

  if (bar && timerEl) {
    var deadline = Date.parse(bar.dataset.deadline);
    var skew = Date.parse(bar.dataset.serverNow) - Date.now();   // server clock minus this device's clock
    var submitted = false;

    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var format = function (ms) {
      var total = Math.ceil(ms / 1000);
      var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
      return (h > 0 ? h + ":" + pad(m) : pad(m)) + ":" + pad(s);
    };

    var tick = function () {
      var left = deadline - (Date.now() + skew);
      if (left <= 0) {
        timerEl.classList.add("over");
        timerEl.textContent = "Time is up — submitting…";
        if (!submitted && finishForm) { submitted = true; finishForm.submit(); }
        return;
      }
      timerEl.textContent = format(left);
      timerEl.classList.toggle("low", left <= 5 * 60 * 1000);      // a calm colour change, no alarms
      setTimeout(tick, 250);
    };
    tick();
  }

  // ---- autosave --------------------------------------------------------------------------------------------
  var form = document.getElementById("save-form");
  if (form) {
    var statusEl = document.getElementById("save-status");
    var url = form.dataset.saveUrl;
    var pending = Promise.resolve();                               // saves go one after another, in order

    var say = function (text) { if (statusEl) statusEl.textContent = text; };

    var applyPalette = function (data) {
      var link = document.getElementById("pal-" + data.entry.pos);
      if (link) {
        link.classList.remove("answered", "unanswered", "notvisited");
        link.classList.add(data.entry.state);
        link.classList.toggle("marked", data.entry.marked);
      }
      var count = document.getElementById("answered-count");
      if (count) count.textContent = data.answered;
    };

    // Every save sends the whole current state, so a save that failed earlier is repaired by the next one.
    var currentState = function () {
      var state = {};
      var answer = form.querySelector('input[name="answer"]:checked');
      var confidence = form.querySelector('input[name="confidence"]:checked');
      var marked = form.querySelector('input[type="checkbox"][name="marked"]');
      if (answer) state.answer = answer.value;
      if (confidence) state.confidence = confidence.value;
      state.marked = marked && marked.checked ? "1" : "0";
      return state;
    };

    var save = function (state) {
      say("Saving…");
      var body = new URLSearchParams(state).toString();
      pending = pending.then(function () {
        return fetch(url, {
          method: "POST", credentials: "same-origin", keepalive: true,
          headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" },
          body: body
        })
          .then(function (r) { return r.json().then(function (data) { return { data: data }; }); })
          .then(function (res) {
            var data = res.data;
            if (data.expired) { window.location.href = data.redirect; return; }
            if (!data.ok) { say(data.error || "Not saved."); return; }
            applyPalette(data);
            say("Saved");
          })
          .catch(function () { say("Not saved — check your connection. It will retry on your next change."); });
      });
    };

    form.addEventListener("change", function () { save(currentState()); });

    var clearBtn = document.getElementById("clear-btn");
    if (clearBtn) {
      clearBtn.addEventListener("click", function (event) {
        event.preventDefault();
        form.querySelectorAll('input[name="answer"], input[name="confidence"]').forEach(function (input) {
          input.checked = false;
        });
        save({ clear: "1" });
      });
    }
    // Without JS the "Save & next" button is needed; with it, everything saves as you go.
    document.querySelectorAll(".save-fallback").forEach(function (b) { b.style.display = "none"; });
    form.addEventListener("submit", function (event) { event.preventDefault(); });
  }

  // ---- palette: open on wide screens, collapsed on phones ---------------------------------------------------
  var palette = document.getElementById("palette");
  if (palette && window.matchMedia("(min-width: 900px)").matches) palette.open = true;
})();
