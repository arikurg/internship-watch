// Tesla internship check: run on tesla.com/careers in your own browser.
//
// Tesla's site only serves real browsers, so the watcher can't fetch it.
// This runs when you click it, as part of your normal visit: it reads the same
// job list the careers page loads, keeps the internships your config.json
// watches would match, drops the ones Simplify already lists as open, and
// shows the rest. "NEW" marks roles this browser hasn't shown you before.
//
// tesla_bookmarklet.html turns this file into a bookmark. Or paste it into the
// browser console (View > Developer > JavaScript Console) on tesla.com.
(async () => {
  const REPO_CONFIG = "https://raw.githubusercontent.com/arikurg/internship-watch/main/config.json";
  const SIMPLIFY = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json";
  const SEEN_KEY = "internship-watch-seen";
  const STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
    "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina",
    "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington", "West Virginia",
    "Wisconsin", "Wyoming", "District of Columbia", "Puerto Rico"];

  const panel = document.getElementById("iw-panel") || document.createElement("div");
  panel.id = "iw-panel";
  panel.style.cssText = "position:fixed;top:16px;right:16px;width:460px;max-height:85vh;overflow:auto;z-index:2147483647;" +
    "background:#fff;color:#111;font:14px/1.4 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;" +
    "border:1px solid #ddd;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.18);padding:14px 16px";
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const show = html => {
    panel.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
      '<b>Tesla internships not on Simplify</b><button id="iw-close" style="border:0;background:none;font-size:18px;cursor:pointer">×</button></div>' + html;
    document.body.appendChild(panel);
    document.getElementById("iw-close").onclick = () => panel.remove();
  };

  if (!/(^|\.)tesla\.com$/.test(location.hostname)) {
    show("<p>Open <a href='https://www.tesla.com/careers/search/'>tesla.com/careers</a> first, then click this again.</p>");
    return;
  }
  show("<p>Checking Tesla and Simplify…</p>");

  try {
    const [tesla, simplify, config] = await Promise.all([
      fetch("/cua-api/apps/careers/state").then(r => { if (!r.ok) throw new Error("Tesla returned HTTP " + r.status); return r.json(); }),
      fetch(SIMPLIFY).then(r => r.json()),
      fetch(REPO_CONFIG, { cache: "no-store" }).then(r => r.json()),
    ]);

    // Filters from config.json: every watch that covers Tesla or any company.
    // Tesla's departments don't line up with Simplify's categories, so only title keywords count.
    const words = list => list && list.length
      ? new RegExp("\\b(?:" + list.map(w => w.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\b", "i")
      : null;
    const watches = config.watches
      .filter(w => w.companies === "any" || (w.companies || []).some(c => c.toLowerCase() === "tesla"))
      .map(w => ({
        name: w.name,
        include: words(w.title_keywords),
        exclude: words(w.title_exclude || config.title_exclude),
      }));
    const skipTerms = new Set((config.skip_terms || []).map(t => t.toLowerCase()));
    const isUS = loc => STATES.some(s => loc === s || loc.endsWith(", " + s));

    // Simplify's Tesla roles, by Tesla job ID.
    const onSimplify = {};
    const dated = [];
    for (const x of simplify) {
      if (x.company_name !== "Tesla") continue;
      const m = (x.url || "").match(/tesla\.com\/careers\/search\/job\/(?:[^/?#]*-)?(\d{5,7})/);
      if (!m) continue;
      onSimplify[m[1]] = x;
      if (x.date_posted) dated.push([+m[1], x.date_posted]);
    }
    dated.sort((a, b) => a[0] - b[0]);
    // Tesla shows no posting dates; its job IDs rise over time, so estimate from nearby IDs.
    const estimate = id => {
      if (!dated.length || id > dated[dated.length - 1][0]) return null;
      let i = dated.findIndex(p => p[0] >= id);
      const near = dated.slice(Math.max(0, i - 3), i + 3).map(p => p[1]).sort((a, b) => a - b);
      const later = Math.min(...dated.slice(i).map(p => p[1]));
      return Math.min(near[Math.floor(near.length / 2)], later);
    };

    const L = tesla.lookup;
    const internTypes = new Set(Object.entries(L.types).filter(([, v]) => /intern/i.test(v)).map(([k]) => k));
    const groups = {};
    for (const x of tesla.listings) {
      if (!internTypes.has(String(x.y)) || !x.t) continue;
      const title = x.t.replace(/\s+/g, " ").trim();
      (groups[title] = groups[title] || []).push(x);
    }

    const seen = new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || "[]"));
    const rows = [];
    for (const [title, xs] of Object.entries(groups)) {
      const ids = xs.map(x => String(x.id));
      const known = ids.map(i => onSimplify[i]).filter(Boolean);
      if (known.some(k => k.active)) continue;
      const locs = [...new Set(xs.map(x => L.locations[x.l]).filter(Boolean))];
      if (config.us_only && locs.length && !locs.some(isUS)) continue;
      const term = (title.match(/\(([^()]*20\d\d[^()]*)\)\s*$/) || [])[1] || "";
      const terms = term.replace(/([A-Za-z])(20\d\d)/g, "$1 $2").split("/").map(s => s.trim()).filter(Boolean);
      const year = (term.match(/20\d\d/g) || []).pop();
      const full = terms.map(s => /20\d\d/.test(s) ? s : s + " " + year).map(s => s.toLowerCase());
      if (skipTerms.size && full.length && full.every(t => skipTerms.has(t))) continue;
      const dept = L.departments[xs[0].dp] || "";
      const watch = watches.find(w =>
        !(w.exclude && w.exclude.test(title)) &&
        (w.include && w.include.test(title)));
      if (!watch) continue;
      const first = ids.map(Number).sort((a, b) => a - b)[0];
      rows.push({
        id: String(first), title: title.replace(/^Internship,\s*/, ""), locs, dept, watch: watch.name,
        posted: estimate(first), isNew: !ids.some(i => seen.has(i)), ids,
        status: known.length ? "Simplify shows it closed" : "Not on Simplify",
      });
    }
    rows.sort((a, b) => (b.isNew - a.isNew) || ((b.posted || 9e12) - (a.posted || 9e12)));

    const fmt = ts => ts ? "posted about " + new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "newer than anything on Simplify";
    const newCount = rows.filter(r => r.isNew).length;
    show(`<p style="margin:0 0 10px;color:#555">${rows.length} matching role${rows.length === 1 ? "" : "s"}` +
      `${newCount ? `, <b>${newCount} new</b> since your last check` : ", none new since your last check"}.</p>` +
      rows.map(r => `<div style="padding:9px 0;border-top:1px solid #eee">` +
        (r.isNew ? '<span style="background:#dcfce7;color:#166534;border-radius:4px;padding:1px 6px;font-size:11px;font-weight:700;margin-right:6px">NEW</span>' : "") +
        `<a href="/careers/search/job/${r.id}" target="_blank" style="color:#1a56db;font-weight:600;text-decoration:none">${esc(r.title)}</a>` +
        `<div style="color:#444;font-size:13px">${esc(r.locs.join(", ") || "Location not listed")}</div>` +
        `<div style="color:#888;font-size:12px">${esc(fmt(r.posted))} · ${esc(r.status)} · ${esc(r.watch)}</div></div>`).join("") +
      '<p style="color:#999;font-size:11px;margin:10px 0 0">Filters come from your internship-watch config.json.</p>');

    rows.forEach(r => r.ids.forEach(i => seen.add(i)));
    localStorage.setItem(SEEN_KEY, JSON.stringify([...seen]));
    window.__iwResult = { count: rows.length, newCount, rows: rows.map(r => `${r.isNew ? "NEW " : ""}${r.title} | ${fmt(r.posted)} | ${r.status}`) };
  } catch (err) {
    show(`<p>Couldn't finish the check: ${esc(err.message)}</p>`);
  }
})();