# internship-watch

A GitHub Action that emails you when a new internship matches one of your watches:

- **Tesla software**: software, firmware, ML and infrastructure roles at Tesla
- **AMD & NVIDIA**: software and AI/ML roles, plus hardware roles only when they're verification
- **Embedded & firmware**: embedded, firmware, RTOS and flight software roles at any company, summer and off-season

It reads the [SimplifyJobs internship feed](https://github.com/SimplifyJobs/Summer2027-Internships), which Simplify refreshes about every 30 minutes and which covers summer and off-season terms. The workflow checks four times an hour, compares matching open roles against `state/seen.json`, and emails you anything it hasn't seen. It also reads Tesla's own careers feed, which catches the many Tesla roles Simplify never lists (see [Direct feeds](#direct-feeds-tesla)).

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

## Direct feeds (Tesla)

Simplify misses a lot. On Nov 21, 2026, about half of Tesla's open US engineering internships weren't on it. With `direct_sources.tesla.enabled` set to `true` in `config.json`, every run also reads Tesla's own listings. Tesla roles that Simplify doesn't list as open go through the same watches and filters, and the email marks them:

- **Not on Simplify**: Simplify has never listed it.
- **Simplify shows it closed**: Simplify marked it closed, but Tesla still lists it.

Marked roles sort to the top of their section, and the subject line counts them.

### Your laptop fetches Tesla's listings

Tesla refuses requests from GitHub's servers (HTTP 403) but answers home and campus connections. So `fetch_tesla.py` runs on your laptop every 15 minutes, downloads Tesla's job feed, keeps the internships, and pushes them as `tesla.json` to a `tesla-feed` branch in this repo. The GitHub Action reads that file (`direct_sources.tesla.feed_url`). The branch always holds a single commit that gets replaced, so it never grows, and pushing it doesn't start a workflow.

Setup, from the repo folder on your laptop:

1. `python3 fetch_tesla.py --check` should print "Tesla accepts requests from this computer." If it prints HTTP 403, Tesla is blocking your network too, and this approach won't work from there.
2. `python3 fetch_tesla.py` publishes once. A `tesla-feed` branch should appear on GitHub.
3. `python3 fetch_tesla.py --install` runs it in the background.
   - **macOS:** installs a launchd job that runs every 15 minutes while the Mac is awake, and right after it wakes. The log is at `~/Library/Logs/internship-watch-tesla.log`.
   - **Linux or Windows:** prints the cron line or `schtasks` command to add.

`python3 fetch_tesla.py --uninstall` removes the background job.

If your laptop sleeps or goes offline, the Action keeps using the last feed for up to 24 hours (`max_age_hours`). After that, you get one email saying the feed is stale, and another once your laptop publishes again. Simplify alerts keep working the whole time.

On macOS, if the log shows "Operation not permitted," the repo is probably in Documents, Desktop or Downloads, which background jobs can't read without extra permission. Move the repo somewhere like `~/code` and run `--install` again.

Details:

- Tesla publishes no posting dates, so the email estimates one from the job ID. Tesla's IDs go up over time, and against 240 Tesla roles with known dates, 80% of estimates landed within 2 days. A role with a newer ID than anything Simplify has shows as "found on the company's site".
- Tesla posts one listing per location. The watcher merges listings with the same title into one entry.
- If Simplify later adds a role you already got from Tesla's site, you won't get a second email.
- If the watcher can't get Tesla's listings for over an hour, you get one email saying why, and another when they come back.
- To turn it off, set `"enabled": false` and run `python3 fetch_tesla.py --uninstall`.

Test it locally against a saved copy of Tesla's feed with `python3 watch.py --dry-run --tesla-file tesla.json`.

## How fast are the alerts?

Simplify publishes at about :02 and :32 past each hour, and the workflow checks at :07, :22, :37 and :52. GitHub sometimes starts scheduled runs late, so expect an email within about 20 minutes of Simplify listing a role. How long Simplify takes to pick up a posting is outside your control.

## When something breaks

- If Gmail rejects the login or the feed is down, the run fails and GitHub emails you about the failed workflow. The watcher sends before it saves, so a failed run loses nothing and the next run retries.
- GitHub pauses scheduled workflows in public repos after 60 days without a commit. The watcher commits a heartbeat after 45 quiet days to prevent that. If the schedule ever stops, re-enable it from the Actions tab.

## Private repo note

Free accounts get 2,000 Actions minutes a month on private repos, and each run bills as at least one minute. Four runs an hour uses about 2,900. For a private repo, change the cron line in `.github/workflows/watch.yml` to `"7,37 * * * *"` (about 1,450 minutes).