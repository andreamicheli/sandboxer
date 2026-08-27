/* Leaderboard — vanilla render da site/data/leaderboard.json
   Vertical bars, logos, no W/D/L in chart, squared, accent fix
*/
(function () {
  "use strict";

  const ACCENT = {
    "deepseek/deepseek-v4-pro": "#2563EB",
    "deepseek/deepseek-v4-flash": "#2563EB",
    "openai/gpt-5.6-luna": "#171717",
    "poolside/laguna-s-2.1-free": "#9A65E8",
    "xiaomi/mimo-v2.5-pro": "#FF6900",
    "meta/muse-spark-1.2-contributor": "#58A9FF",
  };
  // Producer / company badges - squared initials (no model photos)
  const PRODUCER_BADGE = {
    "DeepSeek": { initials: "DS", bg: "#2563EB" },
    "OpenAI":   { initials: "OAI", bg: "#171717" },
    "Poolside": { initials: "PS", bg: "#9A65E8" },
    "Xiaomi":   { initials: "MI", bg: "#FF6900" },
    "Meta":     { initials: "M", bg: "#58A9FF" },
  };
  // Fallback map model_id -> producer when ranking row lacks producer field
  const PRODUCER_BY_MODEL = {
    "deepseek/deepseek-v4-pro": "DeepSeek",
    "deepseek/deepseek-v4-flash": "DeepSeek",
    "openai/gpt-5.6-luna": "OpenAI",
    "poolside/laguna-s-2.1-free": "Poolside",
    "xiaomi/mimo-v2.5-pro": "Xiaomi",
    "meta/muse-spark-1.2-contributor": "Meta",
  };
  const MOBILE_Q = "(max-width: 860px)";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function badgeFor(model_id, producer) {
    var prod = producer || PRODUCER_BY_MODEL[model_id] || "";
    return PRODUCER_BADGE[prod] || { initials: (prod.slice(0, 2).toUpperCase() || "??"), bg: ACCENT[model_id] || "#171717" };
  }
  function logoImg(model_id, cls, producer) {
    var b = badgeFor(model_id, producer);
    var span = el("span", cls + "--fallback", b.initials);
    span.style.background = b.bg;
    span.setAttribute("aria-label", producer || PRODUCER_BY_MODEL[model_id] || model_id);
    span.title = producer || PRODUCER_BY_MODEL[model_id] || "";
    return span;
  }

  function renderChart(mount, ranking) {
    const max = 13;
    const list = el("ol", "lb-chart");
    list.setAttribute("aria-label", "Points by model");

    ranking.forEach(function (row) {
      var col = el("li", "lb-vcol");

      var points = el("div", "lb-vpoints", String(row.points));

      var track = el("div", "lb-vtrack");
      track.setAttribute("role", "img");
      track.setAttribute("aria-label", row.public_name + " " + row.points + " pts");
      var fill = el("div", "lb-vfill");
      var h = max ? (row.points / max * 100) : 0;
      fill.style.height = h.toFixed(1) + "%";
      fill.style.background = ACCENT[row.model_id] || "var(--ink)";
      track.appendChild(fill);

      var label = el("div", "lb-vlabel");
      label.appendChild(logoImg(row.model_id, "lb-vlogo", row.producer));
      var nm = el("div", "lb-vname", row.public_name);
      label.appendChild(nm);
      // no W/D/L, no producer under chart except tooltip aria

      col.appendChild(points);
      col.appendChild(track);
      col.appendChild(label);
      list.appendChild(col);
    });

    mount.replaceChildren(list);
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        var fills = mount.querySelectorAll(".lb-vfill");
        fills.forEach(function (f) {
          var h = f.style.height;
          f.style.height = "0%";
          void f.offsetWidth;
          f.style.height = h;
        });
      });
    });
  }

  function renderTable(mount, ranking) {
    var wrap = el("div", "lb-table-wrap");
    var table = el("table", "lb-table");
    table.setAttribute("aria-label", "League table");

    var thead = document.createElement("thead");
    var hr = document.createElement("tr");
    ["#", "Team", "Pts", "P", "W", "D", "L", "Form"].forEach(function (h, i) {
      var th = el("th", null, h);
      if (h === "Pts") th.title = "Points";
      if (h === "P") th.title = "Played";
      if (h === "W") th.title = "Wins";
      if (h === "D") th.title = "Draws";
      if (h === "L") th.title = "Losses";
      if (i === 0) th.style.width = "32px";
      if (i === 2) th.style.color = "var(--ink)";
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);

    var tbody = document.createElement("tbody");
    ranking.forEach(function (row) {
      var tr = document.createElement("tr");

      var tdPos = el("td", "lb-td-pos", String(row.pos));
      tr.appendChild(tdPos);

      var tdTeam = el("td", "lb-td-team");
      var inner = el("div", "lb-td-team-inner");
      inner.appendChild(logoImg(row.model_id, "lb-td-logo", row.producer));
      var txt = el("div", null);
      var nm = el("div", "lb-td-name", row.public_name);
      var pr = el("div", "lb-td-producer", row.producer);
      txt.appendChild(nm);
      txt.appendChild(pr);
      inner.appendChild(txt);
      tdTeam.appendChild(inner);
      tr.appendChild(tdTeam);

      var tdPt = el("td", "lb-td-pt", String(row.points));
      tr.appendChild(tdPt);

      tr.appendChild(el("td", "lb-td-num", String(row.played)));
      tr.appendChild(el("td", "lb-td-num", String(row.wins)));
      tr.appendChild(el("td", "lb-td-num", String(row.draws)));
      tr.appendChild(el("td", "lb-td-num", String(row.losses)));

      var tdForm = el("td", "lb-td-form");
      var form = el("div", "lb-form");
      (row.form || []).slice(-5).forEach(function (c) {
        form.appendChild(el("i", c, c));
      });
      tdForm.appendChild(form);
      tr.appendChild(tdForm);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    var tie = ranking.find(function (r) { return r.tie_break; });
    if (tie) {
      var foot = el("div", "lb-table-foot", "Note: " + tie.public_name + " — " + tie.tie_break + ".");
      wrap.appendChild(foot);
    }

    mount.replaceChildren(wrap);
  }

  function initTabs() {
    var tabs = document.querySelector(".lb-tabs");
    var btnChart = document.getElementById("lb-tab-chart");
    var btnTable = document.getElementById("lb-tab-table");
    var pChart = document.getElementById("lb-panel-chart");
    var pTable = document.getElementById("lb-panel-table");
    if (!tabs || !btnChart || !btnTable || !pChart || !pTable) return;

    var mql = window.matchMedia(MOBILE_Q);
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var active = "chart";

    function isMobile() { return mql.matches; }

    function syncDesktop() {
      if (!isMobile()) {
        pChart.classList.add("is-active");
        pChart.classList.remove("is-exiting");
        pTable.classList.add("is-active");
        pTable.classList.remove("is-exiting");
        pChart.hidden = false;
        pTable.hidden = false;
        btnChart.setAttribute("aria-selected", "true");
        btnTable.setAttribute("aria-selected", "false");
        tabs.setAttribute("data-active", active);
        return;
      }
      [pChart, pTable].forEach(function (p) { p.hidden = !p.classList.contains("is-active"); });
    }

    function setActive(next, opts) {
      var animate = opts && opts.animate !== false && !reduce && isMobile();
      if (next === active && isMobile()) return;
      active = next;
      tabs.setAttribute("data-active", active);
      var from = active === "chart" ? pTable : pChart;
      var to = active === "chart" ? pChart : pTable;
      var btnFrom = active === "chart" ? btnTable : btnChart;
      var btnTo = active === "chart" ? btnChart : btnTable;

      btnFrom.classList.remove("is-active");
      btnFrom.setAttribute("aria-selected", "false");
      btnTo.classList.add("is-active");
      btnTo.setAttribute("aria-selected", "true");

      if (!isMobile()) {
        syncDesktop();
        return;
      }

      if (!animate) {
        from.classList.remove("is-active", "is-exiting");
        from.hidden = true;
        to.classList.add("is-active");
        to.classList.remove("is-exiting");
        to.hidden = false;
        return;
      }

      from.classList.remove("is-active");
      from.classList.add("is-exiting");
      from.addEventListener("animationend", function handler() {
        from.removeEventListener("animationend", handler);
        from.classList.remove("is-exiting");
        from.hidden = true;
        to.hidden = false;
        void to.offsetWidth;
        to.classList.add("is-active");
      }, { once: true });
    }

    btnChart.addEventListener("click", function () { setActive("chart"); });
    btnTable.addEventListener("click", function () { setActive("table"); });

    tabs.addEventListener("keydown", function (e) {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        var next = e.key === "ArrowRight"
          ? (active === "chart" ? "table" : "chart")
          : (active === "table" ? "chart" : "table");
        setActive(next);
        (next === "chart" ? btnChart : btnTable).focus();
      }
    });

    mql.addEventListener("change", function () {
      syncDesktop();
      if (isMobile()) {
        if (active === "chart") {
          pChart.classList.add("is-active"); pChart.hidden = false;
          pTable.classList.remove("is-active"); pTable.hidden = true;
        } else {
          pTable.classList.add("is-active"); pTable.hidden = false;
          pChart.classList.remove("is-active"); pChart.hidden = true;
        }
      }
    });

    tabs.setAttribute("data-active", active);
    syncDesktop();
    if (isMobile()) {
      pChart.classList.add("is-active"); pChart.hidden = false;
      pTable.classList.remove("is-active"); pTable.hidden = true;
    }
  }

  function render(data) {
    var chartMount = document.getElementById("leaderboard-chart");
    var tableMount = document.getElementById("leaderboard-table");
    var ranking = (data && data.ranking) ? data.ranking.slice().sort(function (a, b) { return a.pos - b.pos; }) : [];
    if (chartMount) renderChart(chartMount, ranking);
    if (tableMount) renderTable(tableMount, ranking);

    var legend = document.getElementById("leaderboard-legend");
    if (legend) {
      if(legend) legend.remove();
    }

    initTabs();
  }

  fetch("data/leaderboard.json", { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("no leaderboard"); return r.json(); })
    .then(render)
    .catch(function () {
      var m = document.getElementById("leaderboard-chart");
      if (m) m.textContent = "Leaderboard unavailable.";
    });
})();
