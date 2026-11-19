# job-digest

A daily email of **new** job postings that match your locations, job titles and experience level. It runs free on GitHub Actions, and it can also keep track of your applications by reading confirmation emails from Gmail.

- Pulls from public job feeds: Greenhouse, Lever and Ashby job boards, plus the [SimplifyJobs](https://github.com/SimplifyJobs/New-Grad-Positions) new-grad list. No scraping or logins.
- Only emails you postings you haven't seen before.
- Finds new companies to follow every week, based on your locations and titles.
- Nothing to install except Python 3.10+.

> [!IMPORTANT]
> **Requires a Gmail account.** The digest is sent through Gmail, and the application tracker reads your Gmail inbox. Outlook, Yahoo, iCloud and other providers aren't supported yet. You need a Google account with 2-Step Verification turned on so you can create an App Password.
>
> The digest can still be *delivered* to any address (see `DIGEST_TO` below). The tracker only sees applications whose confirmation emails arrive in that Gmail inbox.

## Quick start (GitHub Actions, about 10 minutes)

1. **Make your own copy.** Click **Use this template → Create a new repository** at the top of this page. Make it **Private**: your settings and application list get saved there.
2. **Create a Gmail App Password.** Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). 2-Step Verification has to be on. Copy the 16-character password.
3. **Add two secrets.** In your new repo, open **Settings → Secrets and variables → Actions → New repository secret**:
   - `GMAIL_ADDRESS`: your Gmail address
   - `GMAIL_APP_PASSWORD`: the App Password from step 2
   - (optional) `DIGEST_TO`: send the digest to a different address

   Until both Gmail secrets are added, the scheduled daily run skips quietly instead of failing.
4. **Set your preferences.** Either:
   - **On your computer:** clone your repo, run `python -m jobsearch setup`, answer the questions, then commit and push `config.json` and `.github/workflows/daily-digest.yml`. The wizard also sets the email send time.
   - **In the browser:** edit `config.json` on GitHub (pencil icon). See [Settings](#settings) below. If you change `schedule`, also change the two `cron` lines in `.github/workflows/daily-digest.yml` (see [Send time](#send-time)).
5. **Test it.** Go to **Actions → Daily job digest → Run workflow**. You should get an email within a few minutes. After that it runs on its own.

The first digest lists everything currently open from the last 3 weeks. After that you only get new postings.

## Run it on your own computer instead

```
git clone <your repo> && cd job-digest
python -m jobsearch setup                                 # writes config.json
cp .env.example .env                                      # then add your Gmail address + App Password
python -m jobsearch --dry-run --preview preview.html      # see the matches; sends nothing, saves nothing
python -m jobsearch                                       # real run: sends the email
```

To run it daily, schedule `run_daily.bat` (Windows Task Scheduler) or `run_daily.sh` (cron on Mac/Linux).

## Settings

Everything lives in `config.json`:

```json
{
  "locations": ["Chicago", "Austin", "Remote"],
  "roles": {
    "Data": ["data analyst", "data scien*", "analytics engineer"],
    "Product": ["product manag*", "product analyst"]
  },
  "experience": "entry",
  "exclude_words": ["sales", "clearance"],
  "extra_companies": { "greenhouse": ["acme"], "lever": [], "ashby": [] },
  "schedule": { "timezone": "America/Chicago", "hour": 7, "minute": 0 },
  "track_applications": true
}
```

| Setting              | What it does                                                                                                                                                                                                                              |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `locations`          | Cities to match against each posting's location. Add `"Remote"` to include remote roles. Common shorthands are covered: "New York" also matches NYC, Manhattan and Brooklyn, and "San Francisco" also matches SF and Bay Area.            |
| `roles`              | Groups of job titles. Each group becomes a section of the email. Matching ignores case and only matches whole words. Use `*` for the rest of a word (`product manag*`).                                                                   |
| `experience`         | `entry` (new grad / 0–1 years), `mid` (about 2–5 years), `senior`, or `any`. See [How matching works](#how-matching-works).                                                                                                               |
| `exclude_words`      | Titles with any of these words are skipped.                                                                                                                                                                                               |
| `extra_companies`    | More job boards to follow, by the slug in the board's URL: `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`. A starter list of about 40 well-known companies is built in, and weekly discovery adds more. |
| `schedule`           | Local time for the daily email. `timezone` is a name like `America/Los_Angeles` ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)).                                                                                   |
| `track_applications` | Read Gmail (read-only) for application confirmations, interviews and rejections, and add a **Your applications** section to the digest. See [Application tracker](#application-tracker).                                                  |

Users can also write a raw regular expression anywhere a phrase goes by starting it with `re:`, for example `"re:\\bSWE\\b"`. An `"advanced"` object overrides any generated setting, such as `{"advanced": {"max_years_experience": 3}}`. See `jobsearch/prefs.py` for the full list.

### Send time

GitHub's scheduler runs on UTC and doesn't know about daylight saving. So the workflow has two `cron` lines (your local time in winter and in summer), and it only runs the one that matches today. `python -m jobsearch setup` writes them for you. To write them by hand, convert your local time to UTC for standard time and for daylight time. For example, 7:00 AM in New York is `0 12 * * *` and `0 11 * * *`. GitHub usually starts scheduled runs 5–20 minutes late, but runs are unpredictable on a day-by-day basis.

## How matching works

A posting is included when all of these hold:

1. **Location:** it matches one of your `locations`, or it's remote and you included `"Remote"`.
2. **Title:** it matches one of your `roles` and none of the excluded words.
3. **Level:** it fits your `experience` setting:
   - **entry:** skips Senior/Lead/Staff/Director titles, II/III levels, internships and contract roles. The posting must be on the new-grad list, have an entry-level word in the title (Associate, Junior, New Grad, Analyst…), or ask for at most 1 year of experience. Postings that ask for 2 years go in a separate **Stretch Roles** section if they're under 2 weeks old.
   - **mid:** skips Junior/New Grad, Staff/Principal and Director-and-up titles. Accepts postings asking for up to 5 years, with 6–7 years in Stretch roles.
   - **senior:** skips Junior/New Grad and Director-and-up titles.
   - **any:** only skips internships and contract roles.
4. **New:** it hasn't been emailed before. Postings are matched across sources by company and title, so the same job on two boards counts once.

## Application tracker

When `track_applications` is on, each run scans the last few days of your Gmail over IMAP, read-only, using the same App Password. It looks for application confirmations, interview invites and rejections, and keeps `data/applications.csv` up to date (statuses: `drafted`, `applied`, `interview`, `offer`, `rejected`).

- Only company, role, status and date are saved. No email text is kept.
- To fix a company name the tracker gets wrong, add `{"advanced": {"tracker": {"aliases": {"Wrong Name": "Right Name"}}}}`.
- To backfill older applications: `python -m jobsearch --dry-run --scan-days 120` previews what it finds. Drop `--dry-run` to save.
- It only works with Gmail for now.

## Weekly company discovery

Once a week, it collects the Greenhouse, Lever and Ashby boards linked from the SimplifyJobs lists (about 1,900 companies). It tests up to 400 boards it doesn't follow yet, and starts following any that have a matching title in your locations. The new companies are listed in that day's digest and saved in `data/companies.json`. To run discovery right away: `python -m jobsearch --dry-run --discover`.

## Privacy

- Your Gmail password never goes in a file you commit: on GitHub it's a repository secret, and locally it's in `.env`, which is git-ignored.
- After each run, the workflow commits `data/` back to **your** repo: which postings were already sent, your application list, and discovered companies. Keep your copy private if you don't want those visible.

## Command reference

```
python -m jobsearch setup                 # interactive setup
python -m jobsearch --dry-run             # show matches; send nothing, save nothing
python -m jobsearch --preview FILE        # also write the email HTML to FILE
python -m jobsearch --send-empty          # send even if nothing is new (test email setup)
python -m jobsearch --reset               # forget what's been sent; next run acts like the first
python -m jobsearch --discover            # run company discovery now
python -m jobsearch --scan-days N         # scan N days of Gmail for application emails
python -m unittest                        # run the tests
```

## Credits

New-grad listings and the company list for discovery come from [SimplifyJobs](https://github.com/SimplifyJobs/New-Grad-Positions). Job data belongs to the companies posting it. MIT licensed.
