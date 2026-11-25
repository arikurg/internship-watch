# internship-watch

A GitHub Action that emails you when a new internship matches one of your watches:

- **Tesla software**: software, firmware, ML and controls roles at Tesla (including Tesla roles Simplify misses, via the Chrome extension)
- **Chip companies** (AMD, NVIDIA, Intel, Marvell, Tenstorrent, Lightmatter): software and AI/ML roles, plus hardware roles when the title says verification, compiler, test automation or platform
- **Amazon, Meta & Intuit**: software engineering roles
- **Neuralink**: software roles
- **Space, defense, robotics & infra**: software, firmware, flight software, avionics, GNC, controls, SRE and autonomy roles at Astranis, Anduril, Varda, Zipline, Figure, Nuro, Cloudflare, SpaceX, Etched and Kodiak
- **Embedded & firmware**: embedded, firmware, RTOS and flight software roles at any company, summer and off-season

It reads the [SimplifyJobs internship feed](https://github.com/SimplifyJobs/Summer2027-Internships), which Simplify refreshes about every 30 minutes and which covers summer and off-season terms. The workflow checks four times an hour, compares matching open roles against `state/seen.json`, and emails you anything it hasn't seen. Tesla roles Simplify never lists come in through a small Chrome extension (see [Tesla roles Simplify misses](#tesla-roles-simplify-misses)).

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

Broadening a filter makes older open roles count as new, so expect one bigger email after the change. Two easy additions if you want them: `"validation"` in the chip companies keywords for post-silicon validation roles, and `"fpga"` in the embedded watch for FPGA roles.

Preview what your filter matches without sending anything:

```sh
python3 watch.py --dry-run --test-email
```

## Companies read straight from Greenhouse

Many companies post jobs on Greenhouse, whose job board feed is public and built for programs to read, so the watcher reads those boards directly. Simplify misses a lot of them. On Nov 23, 2026, none of Neuralink's 5 software intern roles were on Simplify, and Varda, Astranis, Anduril and Zipline each had several internships Simplify didn't list.

The boards live in `config.json` under `direct_sources.greenhouse.boards`, as company name → board name:

```json
"greenhouse": {
  "enabled": true,
  "boards": { "Neuralink": "neuralink", "Astranis": "astranis", "SpaceX": "spacex" }
}
```

- **Board name:** the part after `greenhouse.io/` in the company's job links (`job-boards.greenhouse.io/astranis/jobs/…` → `astranis`). You can test one in your browser: `https://boards-api.greenhouse.io/v1/boards/astranis/jobs` should show a list of jobs.
- **Company name:** spell it the way Simplify does (for example "Varda Space", "Kodiak Robotics"), and add it to a watch's `companies` so its roles get filtered. The "Space, defense, robotics & infra" watch covers most of these boards.
- Only internships and co-ops are read. Roles Simplify lists as open are skipped (Simplify's copy covers them); the rest are marked **Not on Simplify** or **Simplify shows it closed**, with Greenhouse's real posting date.
- For US-only filtering, Greenhouse roles are dropped only when every location is clearly outside the US, since many boards list bare city names like "San Francisco".
- If a board stops working (renamed, or the company left Greenhouse), you get one email naming it; the other boards keep working.

Tenstorrent posts student roles on a separate board, `tenstorrentuniversity`; its main `tenstorrent` board has no internships. Lightmatter's board mostly holds hardware internships, so the chip watch keeps only the software, platform and test automation ones.

Some companies keep evergreen intern postings open for a year or more (Neuralink's date to 2024 and 2025), so expect old posting dates on those.

## Tesla roles Simplify misses

Simplify misses a lot of Tesla. On Nov 21, 2026, about half of Tesla's open US engineering internships weren't on it. Tesla's careers site has them all, but it only serves real browsers: scripts get HTTP 403 whether they run on GitHub's servers or a home laptop. So Tesla gets read from inside your Chrome, two ways.

### Email alerts: the Chrome extension

`tesla-extension/` is a small Chrome extension. Every 3 hours while you're using your computer, it reads Tesla's internship list (about 300 KB) and publishes it to a private GitHub Gist. The watcher reads the Gist, runs Tesla roles through your watches, and emails new ones marked **Not on Simplify** or **Simplify shows it closed**, with no duplicates if Simplify adds them later.

Setup (about 5 minutes):

1. **Make a token** that can only edit gists: GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token. Under **Account permissions**, set **Gists** to *Read and write*. Leave repository access at the default. It can't touch your repos.
2. **Load the extension:** open `chrome://extensions`, turn on **Developer mode** (top right), click **Load unpacked**, and choose the `tesla-extension` folder.
3. **Connect it:** the options page opens. Paste the token and click **Save and check now**. It creates a private gist and shows a `config.json` snippet.
4. **Turn it on in the watcher:** in `config.json`, replace the `"tesla"` entry under `direct_sources` with that snippet, then push.

The next watcher run emails every matching Tesla role that isn't open on Simplify. After that, you only hear about new ones.

Good to know:

- It runs while Chrome is running and you're using the computer. A check that comes due while you're away or the screen is locked waits until you're back. Change the interval (1, 3 or 6 hours) on the extension's options page.
- It first tries a background request that carries your Tesla cookies. If Tesla refuses that (it refused the cookie-less version with HTTP 403), it does what you picked on the options page:
  - **Wait for a visit** (default): nothing opens. The next time you open tesla.com/careers yourself, it reads the list from that page.
  - **Minimized window**: it loads Tesla's careers page in a minimized window for a few seconds, reads the list, and closes the window. Fully automatic.
- Opening tesla.com/careers yourself also finishes setup if the first check is refused: the extension creates your Gist from that page.
- When nothing changed, it skips publishing, and it republishes at least every 12 hours so the watcher knows it's alive. If the Gist goes 48 hours without an update (`max_age_hours`), you get one email about it, and Simplify alerts keep working.
- Click the extension's icon to see the last check. A red **!** means the last check failed.
- Tesla publishes no posting dates, so emails estimate them from job IDs, which go up over time. Against 240 Tesla roles with known dates, 80% of estimates landed within 2 days.

### Manual check: the bookmark

`tesla_bookmarklet.html` installs a **Tesla check** bookmark. On tesla.com/careers, click it to see the same matching roles in a panel, with **NEW** on ones that browser hasn't shown you. It reads your filters from `config.json` on GitHub, so push changes before relying on them.

## How fast are the alerts?

Simplify publishes at about :02 and :32 past each hour, and the workflow checks at :07, :22, :37 and :52. GitHub sometimes starts scheduled runs late, so expect an email within about 20 minutes of Simplify listing a role. How long Simplify takes to pick up a posting is outside your control.

## When something breaks

- If Gmail rejects the login or the feed is down, the run fails and GitHub emails you about the failed workflow. The watcher sends before it saves, so a failed run loses nothing and the next run retries.
- GitHub pauses scheduled workflows in public repos after 60 days without a commit. The watcher commits a heartbeat after 45 quiet days to prevent that. If the schedule ever stops, re-enable it from the Actions tab.

## Private repo note

Free accounts get 2,000 Actions minutes a month on private repos, and each run bills as at least one minute. Four runs an hour uses about 2,900. For a private repo, change the cron line in `.github/workflows/watch.yml` to `"7,37 * * * *"` (about 1,450 minutes).