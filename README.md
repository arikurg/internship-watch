# internship-watch

A GitHub Action that emails you when a new Tesla software internship shows up.

It reads the [SimplifyJobs internship feed](https://github.com/SimplifyJobs/Summer2027-Internships), which Simplify refreshes about every 30 minutes and which covers summer and off-season terms. The workflow checks four times an hour, compares open Tesla roles against `state/seen.json`, and emails you anything it hasn't seen. Tesla's careers site blocks most automated traffic, so the watcher doesn't touch it.

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

Edit `config.json`:

- `companies`: add names exactly as Simplify spells them, e.g. `["Tesla", "SpaceX", "NVIDIA"]`.
- `always_include_categories`: Simplify categories that always count. The default takes everything Simplify files under Software.
- `title_keywords`: words that pull in roles from other categories. Simplify files many Tesla SWE roles (firmware, embedded, vehicle software) under Hardware, so the keyword list catches those.
- `title_exclude`: words that drop a role even if it matched, e.g. `["analyst"]`.
- `feed_url`: when Simplify starts its Summer 2028 repo, point this at the new repo's `.github/scripts/listings.json`.

Broadening the filter makes older open roles count as new, so expect one bigger email after the change.

Preview what your filter matches without sending anything:

```sh
python3 watch.py --dry-run --test-email
```

## How fast are the alerts?

Simplify publishes at about :02 and :32 past each hour, and the workflow checks at :07, :22, :37 and :52. GitHub sometimes starts scheduled runs late, so expect an email within about 20 minutes of Simplify listing a role. How long Simplify takes to pick up a Tesla posting is outside your control.

## When something breaks

- If Gmail rejects the login or the feed is down, the run fails and GitHub emails you about the failed workflow. The watcher sends before it saves, so a failed run loses nothing and the next run retries.
- GitHub pauses scheduled workflows in public repos after 60 days without a commit. The watcher commits a heartbeat after 45 quiet days to prevent that. If the schedule ever stops, re-enable it from the Actions tab.

## Private repo note

Free accounts get 2,000 Actions minutes a month on private repos, and each run bills as at least one minute. Four runs an hour uses about 2,900. For a private repo, change the cron line in `.github/workflows/watch.yml` to `"7,37 * * * *"` (about 1,450 minutes).
