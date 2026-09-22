/* Hover / focus tooltips for the server-drawn charts. Progressive enhancement only: every chart is readable
 * without this (values are labelled on the marks and every chart has a table). Nothing here reads or changes data.
 *
 * Marks carry data-tip="value | detail". The value leads (strong), the detail follows (secondary). The text is set
 * with textContent, never as HTML, because names in the tips come from data. */
(function () {
  "use strict";

  var tip = document.createElement("div");
  tip.className = "viz-tip";
  tip.hidden = true;
  tip.setAttribute("role", "tooltip");
  document.body.appendChild(tip);

  function fill(text) {
    var parts = String(text).split(" | ");
    tip.textContent = "";
    var value = document.createElement("strong");
    value.textContent = parts[0];
    tip.appendChild(value);
    if (parts.length > 1) {
      var detail = document.createElement("span");
      detail.textContent = parts.slice(1).join(" | ");
      tip.appendChild(detail);
    }
  }

  function place(clientX, clientY) {
    var pad = 14;
    tip.hidden = false;
    var rect = tip.getBoundingClientRect();
    var x = clientX + pad;
    var y = clientY - rect.height - pad;
    if (x + rect.width > window.innerWidth - 8) x = clientX - rect.width - pad;
    if (y < 8) y = clientY + pad;
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = y + "px";
  }

  function crosshair(mark, on) {
    var svg = mark.ownerSVGElement;
    var line = svg && svg.querySelector(".viz-crosshair");
    if (!line) return;
    var x = mark.getAttribute("data-x");
    if (on && x !== null) {
      line.setAttribute("x1", x);
      line.setAttribute("x2", x);
      line.classList.add("on");
    } else {
      line.classList.remove("on");
    }
  }

  function show(mark, clientX, clientY) {
    fill(mark.getAttribute("data-tip") || "");
    place(clientX, clientY);
    crosshair(mark, true);
  }

  function hide(mark) {
    tip.hidden = true;
    crosshair(mark, false);
  }

  document.querySelectorAll(".viz-mark").forEach(function (mark) {
    mark.addEventListener("pointermove", function (event) { show(mark, event.clientX, event.clientY); });
    mark.addEventListener("pointerleave", function () { hide(mark); });
    // Keyboard focus shows exactly what hover shows.
    mark.addEventListener("focus", function () {
      var box = mark.getBoundingClientRect();
      show(mark, box.left + box.width / 2, box.top);
    });
    mark.addEventListener("blur", function () { hide(mark); });
  });
})();
