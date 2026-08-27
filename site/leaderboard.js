/* Leaderboard — vanilla render da site/data/leaderboard.json
   Desktop: side-by-side shadcn BarChart (div-based) + football table
   Mobile: animated Tabs (Grafico / Classifica) with slide indicator + panel animation
   Design system: Fraunces, --ink #171717, --line #d9d9d9, --bg white
*/
(function () {
  "use strict";

  const ACCENT = {
    "deepseek/deepseek-v4-pro": "#4F46E5",
    "openai/gpt-5.6-luna": "#10A37F",
    "poolside/laguna-s-2.1-free": "#9A65E8",
    "xiaomi/mimo-v2.5-pro": "#FF6900",
    "deepseek/deepseek-v4-flash": "#0EA5E9",
    "meta/muse-spark-1.2-contributor": "#58A9FF",
  };
  const MOBILE_Q = "(max-width: 860px)";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function renderChart(mount, ranking) {
    const max = Math.max.apply(null, ranking.map(function (r) { return r.points; })) || 13;
    const list = el("ol", "lb-chart");
    list.setAttribute("aria-label", "Grafico punti per modello");

    var head = el("li", "lb-row lb-row--head");
    head.setAttribute("aria-hidden", "true");
    head.appendChild(el("span", null, "Modello"));
    head.appendChild(el("span", null, "Punti"));
    head.appendChild(el("span", null, ""));
    list.appendChild(head);

    ranking.forEach(function (row) {
      var li = el("li", "lb-row");

      var label = el("div", "lb-label");
      var rank = el("span", "lb-rank" + (row.pos === 1 ? " lb-rank--1" : ""), String(row.pos));
      var nameWrap = el("div", null);
      var name = el("div", "lb-name", row.public_name);
      var prod = el("span", "lb-producer", row.producer);
      nameWrap.appendChild(name);
      nameWrap.appendChild(prod);
      label.appendChild(rank);
      label.appendChild(nameWrap);

      var track = el("div", "lb-track");
      track.setAttribute("role", "img");
      track.setAttribute("aria-label", row.public_name + " " + row.points + " punti");
      var fill = el("div", "lb-fill");
      fill.style.width = (row.points / max * 100).toFixed(1) + "%";
      fill.style.background = ACCENT[row.model_id] || "var(--ink)";
      track.appendChild(fill);

      var right = el("div", null);
      var points = el("div", "lb-points", String(row.points));
      right.appendChild(points);
      if (row.form && row.form.length) {
        var form = el("div", "lb-form");
        row.form.forEach(function (c) {
          form.appendChild(el("i", c, c));
        });
        right.appendChild(form);
      }

      li.appendChild(label);
      li.appendChild(track);
      li.appendChild(right);
      list.appendChild(li);
    });

    mount.replaceChildren(list);
    // animate fill after paint for 700ms transition
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        var fills = mount.querySelectorAll(".lb-fill");
        fills.forEach(function (f) {
          var w = f.style.width;
          f.style.width = "0%";
          // force reflow
          void f.offsetWidth;
          f.style.width = w;
        });
      });
    });
  }

  function renderTable(mount, ranking) {
    var wrap = el("div", "lb-table-wrap");
    var table = el("table", "lb-table");
    table.setAttribute("aria-label", "Classifica stile calcio");

    var thead = document.createElement("thead");
    var hr = document.createElement("tr");
    ["#", "Squadra", "Pt", "G", "V", "N", "P", "Forma"].forEach(function (h, i) {
      var th = el("th", null, h);
      if (h === "Pt") th.title = "Punti";
      if (h === "G") th.title = "Giocate";
      if (h === "V") th.title = "Vinte";
      if (h === "N") th.title = "Pareggiate";
      if (h === "P") th.title = "Perse";
      // align
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
      var nm = el("div", "lb-td-name", row.public_name);
      var pr = el("div", "lb-td-producer", row.producer);
      tdTeam.appendChild(nm);
      tdTeam.appendChild(pr);
      tr.appendChild(tdTeam);

      var tdPt = el("td", "lb-td-pt", String(row.points));
      tr.appendChild(tdPt);

      tr.appendChild(el("td", "lb-td-num", String(row.played)));
      tr.appendChild(el("td", "lb-td-num", String(row.wins)));
      tr.appendChild(el("td", "lb-td-num", String(row.draws)));
      tr.appendChild(el("td", "lb-td-num", String(row.losses)));

      var tdForm = el("td", "lb-td-form");
      var form = el("div", "lb-form lb-form--table");
      (row.form || []).slice(-5).forEach(function (c) {
        form.appendChild(el("i", c, c));
      });
      tdForm.appendChild(form);
      tr.appendChild(tdForm);

      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    // tie-break footnote if present
    var tie = ranking.find(function (r) { return r.tie_break; });
    if (tie) {
      var foot = el("div", "lb-table-foot", "Nota: " + tie.public_name + " — " + tie.tie_break + ".");
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
        // desktop: both visible, no animation, indicator irrelevant
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
      // mobile: only active visible
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

      // animate exit then enter
      from.classList.remove("is-active");
      from.classList.add("is-exiting");
      from.addEventListener("animationend", function handler() {
        from.removeEventListener("animationend", handler);
        from.classList.remove("is-exiting");
        from.hidden = true;
        to.hidden = false;
        // trigger enter
        void to.offsetWidth;
        to.classList.add("is-active");
      }, { once: true });
    }

    btnChart.addEventListener("click", function () { setActive("chart"); });
    btnTable.addEventListener("click", function () { setActive("table"); });

    // keyboard: arrow left/right
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
      // reset to chart as default on breakpoint change, but respect current active
      syncDesktop();
      if (isMobile()) {
        // ensure only active shown
        if (active === "chart") {
          pChart.classList.add("is-active"); pChart.hidden = false;
          pTable.classList.remove("is-active"); pTable.hidden = true;
        } else {
          pTable.classList.add("is-active"); pTable.hidden = false;
          pChart.classList.remove("is-active"); pChart.hidden = true;
        }
      }
    });

    // initial
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
      var max = ranking.length ? Math.max.apply(null, ranking.map(function (r) { return r.points; })) : 13;
      legend.innerHTML = "<b>" + max + " pt</b> max · Punteggio: <b>3</b> vittoria · <b>1</b> pareggio · <b>0</b> sconfitta · " + (data.series_count || 15) + " serie best-of-3 · round-robin 6 modelli";
    }

    initTabs();
  }

  fetch("data/leaderboard.json", { cache: "no-store" })
    .then(function (r) { if (!r.ok) throw new Error("no leaderboard"); return r.json(); })
    .then(render)
    .catch(function () {
      var m = document.getElementById("leaderboard-chart");
      if (m) m.textContent = "Classifica non disponibile.";
    });
})();
