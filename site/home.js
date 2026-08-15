/* Date-gated homepage indexing for the Sandboxer broadcast layer.
 *
 * The site is a static deploy: a result is stored in data/publications.json
 * with a `publish_at` date (an odd calendar day) and is only revealed — the
 * sober top banner and the homepage article list — once that date arrives in
 * the visitor's local timezone. The upload itself is unlisted on YouTube and
 * can be previewed at any time via its direct link.
 */
(function () {
  "use strict";

  function todayISO() {
    const now = new Date();
    const y = now.getFullYear();
    const m = String(now.getMonth() + 1).padStart(2, "0");
    const d = String(now.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
  }

  function visible(items, now) {
    return items.filter(function (item) {
      return !item.publish_at || item.publish_at <= now;
    });
  }

  function renderBanner(latest) {
    const banner = document.getElementById("announcement-banner");
    if (!banner || !latest) return;
    const link = banner.querySelector("a");
    link.textContent = latest.banner;
    if (latest.report_url) link.href = latest.report_url;
    else if (latest.video_url) link.href = latest.video_url;
    banner.hidden = false;
  }

  function renderArticles(items) {
    const list = document.getElementById("published-results");
    if (!list) return;
    if (!items.length) {
      list.hidden = true;
      return;
    }
    list.replaceChildren(
      ...items.map(function (item) {
        const li = document.createElement("li");
        const title = document.createElement("span");
        title.textContent = item.title;
        li.appendChild(title);
        const links = document.createElement("span");
        links.className = "published-links";
        if (item.report_url) {
          const report = document.createElement("a");
          report.href = item.report_url;
          report.textContent = "Report";
          links.appendChild(report);
        }
        if (item.video_url) {
          const video = document.createElement("a");
          video.href = item.video_url;
          video.textContent = "Watch";
          links.appendChild(video);
        }
        li.appendChild(links);
        return li;
      })
    );
    list.hidden = false;
  }

  fetch("data/publications.json", { cache: "no-store" })
    .then(function (response) {
      if (!response.ok) throw new Error("publications unavailable");
      return response.json();
    })
    .then(function (data) {
      const items = Array.isArray(data.publications) ? data.publications : [];
      const now = todayISO();
      const live = visible(items, now);
      const latest = live.length
        ? live.slice().sort(function (a, b) {
            return (b.publish_at || "").localeCompare(a.publish_at || "");
          })[0]
        : null;
      renderBanner(latest);
      renderArticles(live);
    })
    .catch(function () {
      /* Fail closed: no banner, no article list. */
    });
})();
