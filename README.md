# internship-watch

A GitHub Action that emails you when a new internship matches one of your watches:

- **Tesla software**: software, firmware, ML and controls roles at Tesla (plus a one-click browser check for Tesla roles Simplify misses)
- **AMD & NVIDIA**: software and AI/ML roles, plus hardware roles only when they're verification
- **Embedded & firmware**: embedded, firmware, RTOS and flight software roles at any company, summer and off-season

It reads the [SimplifyJobs internship feed](https://github.com/SimplifyJobs/Summer2027-Internships), which Simplify refreshes about every 30 minutes and which covers summer and off-season terms. The workflow checks four times an hour, compares matching open roles against `state/seen.json`, and emails you anything it hasn't seen. For Tesla roles Simplify never lists, there's a one-click check you run in your browser (see [Tesla roles Simplify misses](#tesla-roles-simplify-misses)).

## Setup (about 10 minutes)

1. **Create a public repo** and push these files to it. Public repos get unlimited free Actions minutes. (Private repo? See the note below.) From inside this folder, with the GitHub CLI:

   ```sh
   git init -b main && git add . && git commit -m "Internship watcher"
   gh repo create internship-watch --public --source . --push
   ```

2. **Make a Gmail app password.** On the Gmail account you want to send from, turn on 2-Step Verification, then create an app password at <https://myaccount.google.com/apppasswords>. Google shows you a 16-character code once.

3. **Add three secrets.** In the repo, open Settings → Secrets and variables → Actions → New repository secret:

   | Name | Value |
   |---|---|
   | `SMTP_USER` | the Gmail address you send from |
   | `SMTP_PASSWORD` | the app password, spaces removed |
   | `EMAIL_TO` | where alerts go (comma-separate several addresses) |

4. **Run it once by hand.** Open the Actions tab → Watch internships → Run workflow. The first run emails you every matching role that's open now and commits `state/seen.json`. From then on you only get emails for new postings.

After that the schedule takes over. To check your email setup again, run the workflow with **test_email** ticked. It sends the current list and saves nothing.

## Changing what it watches

Edit `config.json`. Each entry in `watches` is one rule, and a role that matches any watch gets emailed (once, even if it matches several). Emails group roles under the watch that caught them.

Inside a watch:

- `name`: the section heading in your emails.
- `companies`: names exactly as Simplify spells them, e.g. `["SpaceX", "Anduril"]`, or `"any"` for every company.
- `categories`: Simplify categories that always count (`Software`, `Software Engineering`, `Hardware`, `AI/ML/Data`, `Product`, `Quant`, ...).
- `title_keywords`: words that pull in a role from any category. They match whole words, case-insensitive. Simplify files many software roles (firmware, embedded, vehicle software) under Hardware, so keywords catch what categories miss.
- `title_exclude` and `degrees_any` (optional): override the top-level values for this watch only.

Top-level settings apply to every watch:

- `title_exclude`: words that drop a role even if it matched, e.g. `["analyst"]`.
- `degrees_any`: a role passes if it accepts at least one of these degrees. The default `["Bachelor's", "Master's"]` drops PhD-only and MBA roles. Roles that list no degree always pass.
- `us_only`: when `true`, drop roles whose locations are all outside the US. A role that lists any US site (or "Remote") still passes. A watch can set its own `us_only` to override this.
- `skip_terms`: drop a role when every term it lists is here. The default skips 2026 terms that have already started, which Simplify sometimes leaves marked open. Roles with no term listed always pass.
- `feed_url`: when Simplify starts its Summer 2028 repo, point this at the new repo's `.github/scripts/listings.json`.

Broadening a filter makes older open roles count as new, so expect one bigger email after the change. Two easy additions if you want them: `"validation"` in the AMD & NVIDIA keywords for post-silicon validation roles, and `"fpga"` in the embedded watch for FPGA roles.

Preview what your filter matches without sending anything:

```sh
python3 watch.py --dry-run --test-email
```

## Tesla roles Simplify misses

Simplify misses a lot of Tesla. On Nov 21, 2026, about half of Tesla's open US engineering internships weren't on it. Tesla's careers site has them all, but it only serves real browsers: scripts get HTTP 403 whether they run on GitHub's servers or a home laptop. So the watcher doesn't fetch Tesla. You check it in your own browser with one click instead.

**Set up the Tesla check:** open `tesla_bookmarklet.html` from this repo in your browser and drag the **Tesla check** button onto your bookmarks bar. Then, on [tesla.com/careers](https://www.tesla.com/careers/search/), click the bookmark. A panel lists Tesla internships that:

- match the Tesla and any-company watches in your `config.json` (title keywords, `title_exclude`, `us_only`, `skip_terms`), and
- Simplify doesn't list as open (marked "Not on Simplify" or "Simplify shows it closed").

**NEW** marks roles that browser hasn't shown you before. Tesla publishes no posting dates, so the panel estimates them from job IDs, which go up over time. Against 240 Tesla roles with known dates, 80% of estimates landed within 2 days.

The check reads your filters from `config.json` on GitHub, so push config changes before relying on them. If your browser won't run the bookmark, paste `tesla_check.js` into the console on tesla.com.

`watch.py` still has code for reading Tesla directly (`direct_sources.tesla` in `config.json`), switched off. It works again if Tesla ever opens its feed to scripts.

## How fast are the alerts?

Simplify publishes at about :02 and :32 past each hour, and the workflow checks at :07, :22, :37 and :52. GitHub sometimes starts scheduled runs late, so expect an email within about 20 minutes of Simplify listing a role. How long Simplify takes to pick up a posting is outside your control.

## When something breaks

- If Gmail rejects the login or the feed is down, the run fails and GitHub emails you about the failed workflow. The watcher sends before it saves, so a failed run loses nothing and the next run retries.
- GitHub pauses scheduled workflows in public repos after 60 days without a commit. The watcher commits a heartbeat after 45 quiet days to prevent that. If the schedule ever stops, re-enable it from the Actions tab.

## Private repo note

Free accounts get 2,000 Actions minutes a month on private repos, and each run bills as at least one minute. Four runs an hour uses about 2,900. For a private repo, change the cron line in `.github/workflows/watch.yml` to `"7,37 * * * *"` (about 1,450 minutes).