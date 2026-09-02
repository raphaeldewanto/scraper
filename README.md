# PIC Registry

A landing page (GitHub Pages) + a GitHub Actions workflow that does the
actual scraping. Fill in a form on GitHub, wait ~1–2 minutes, refresh
the page — new contacts show up in the directory and in `results.csv`.

## Why it's built this way

GitHub Pages only serves static files — it can't run Python or fetch
arbitrary external sites on its own (browsers block that kind of
cross-site scraping from JavaScript). So the "form" people fill in
isn't on the landing page itself; it's **GitHub's own Actions form**,
which appears automatically when you click "Run workflow." This
avoids the alternative (a JavaScript form that calls the GitHub API
directly), which would require embedding an access token in public
page code — anyone visiting the site could then steal it. Actions
inputs stay server-side and need no token to expose.

## 1. Publish this to GitHub

```bash
# from inside this folder
git init
git add .
git commit -m "Initial commit: PIC Registry"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

## 2. Turn on GitHub Pages

Repo → **Settings → Pages** → under "Build and deployment", set
**Source: Deploy from a branch**, branch **main**, folder **/ (root)**.
Save. Your site will be live at
`https://YOUR-USERNAME.github.io/YOUR-REPO/` within a minute or two.

## 3. Let the workflow push results back to the repo

Repo → **Settings → Actions → General** → scroll to "Workflow
permissions" → select **Read and write permissions** → Save.
(Without this, the workflow can scrape but can't commit the results.)

## 4. Point the page at your repo

Open `index.html`, find this near the bottom:

```js
const GITHUB_USER = "YOUR-GITHUB-USERNAME";
const GITHUB_REPO = "YOUR-REPO-NAME";
```

Fill in your actual username and repo name, commit, and push. This is
what makes the "Open the scrape form on GitHub" button point to the
right place.

## 5. Run your first scrape

Go to your repo's **Actions** tab → **Run PIC Scraper** (left
sidebar) → **Run workflow** button (top right). You'll see a form
with these fields:

- **sector** — scrape everything already saved under this sector
  (e.g. `Environmental NGO`). Leave blank if you're adding a new org
  instead.
- **org_name / org_sector / team_url / contact_url** — fill these in
  to scrape one brand-new organization. It gets saved into
  `config.json` automatically, so next time you can just type its
  sector into the field above (or it's picked up by the weekly
  scheduled run).
- **render_js** — check this if the org's site is built with
  React/Vue/Next.js (view page source in your browser; if the team
  names aren't in the raw HTML, it's JS-rendered).
- **save_to_library** — on by default; turns off if you want a
  one-off scrape without adding the org permanently.

Click **Run workflow**. It takes about 1–2 minutes. When it's done,
refresh your GitHub Pages site — the directory table and the "last
synced" timestamp update automatically from `data/results.csv`.

## 6. Ongoing / recurring scraping

Already set up: the workflow also runs automatically every **Monday
02:00 UTC**, re-scraping everything currently saved in `config.json`.
Edit the `cron` line in `.github/workflows/scrape.yml` to change the
schedule.

## What's published vs. what isn't

Both IRID and Kopernik — and most NGOs/corporates — publish staff
**names and titles**, but only a **general org email** (not personal
ones). This tool reports only what's actually published; it doesn't
guess personal emails from name patterns, since that produces
unverified data and edges toward spam-list building.

## Legal/practical notes (worth keeping in mind since this runs on a schedule)

- Only scrapes information organizations chose to publish for
  outreach purposes — not personal social profiles.
- Check `robots.txt` on any new domain before adding it (e.g.
  `example.com/robots.txt`).
- If you're in Indonesia, this data falls under UU PDP (Personal Data
  Protection Law). Keep a record of *why* you're collecting it
  (legitimate interest for B2B/NGO outreach is a reasonable basis),
  and be ready to remove someone's data on request.
- The `User-Agent` string in `scraper.py` has a placeholder contact
  email — swap in a real one so site owners can reach you if they
  have questions. Good scraping etiquette.

## Known limitations

- Team-page name/role extraction is heuristic (heading tags + "Name –
  Role" patterns). Expect to spot-check results on sites with very
  different layouts from IRID's.
- Phone regex is tuned for Indonesian mobile formats plus generic
  dashed formats.
- JS-heavy sites add ~30–60 seconds per run for the headless browser.

## Local testing (optional)

```bash
pip install -r requirements.txt
playwright install chromium
python scraper.py --sector "Environmental NGO"
```
