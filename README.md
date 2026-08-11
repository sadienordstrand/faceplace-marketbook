# Faceplace Marketbook

Facebook Marketplace's built-in search only covers one city at a time, and pads
the results with unrelated listings.

**This tool:**
- Searches as many cities as you like (the default is twelve cities with search radiuses that cover the entire continental US)
- Throws out the listings that don't match, so you don't have to sift through all the junk
- Collects each listing's photo and description, and builds a catalogue you can
  search, sort, and prune
- If desired, will automatically run scheduled searches and email you what's new since last time.

Works on Windows and Mac.

## Contents

- [**Please read this first**](#please-read-this-first)
- [**Overview**](#overview)
- [**Setting it up**](#setting-it-up)
  - [Step 1 — Get the folder onto your computer](#step-1--get-the-folder-onto-your-computer)
  - [Step 2 — Start it](#step-2--start-it)
  - [Step 3 — Log into Facebook](#step-3--log-into-facebook)
  - [Step 4 — Set your search radius](#step-4--set-your-search-radius)
  - [Step 5 — Add a shortcut on your computer, if you like](#step-5--add-a-shortcut-on-your-computer-if-you-like)
- [**Running a search**](#running-a-search)
  - [Getting better results](#getting-better-results)
  - [Adding your own cities](#adding-your-own-cities)
- [**While it runs**](#while-it-runs)
- [**Your results**](#your-results)
- [**Automated searches**](#automated-searches)
  - [On a Mac, check where this folder lives first](#on-a-mac-check-where-this-folder-lives-first)
  - [Part 1 — Give the app an email password](#part-1--give-the-app-an-email-password)
  - [Part 2 — Save a search](#part-2--save-a-search)
  - [Part 3 — Let your computer wake itself up](#part-3--let-your-computer-wake-itself-up)
  - [What the emails look like](#what-the-emails-look-like)
  - [Where scheduled results live](#where-scheduled-results-live)
  - [Turning it off](#turning-it-off)
- [**Keeping it up to date**](#keeping-it-up-to-date)
- [**Troubleshooting**](#troubleshooting)
- [**For the technically inclined**](#for-the-technically-inclined)

---

## Please read this first

**Automating Facebook is against their Terms of Service.** This tool runs on
your own real Facebook account, using your own login. There is a chance that
Facebook may restrict or ban your account for bot activity. By using this tool,
you acknowledge and accept this risk.

**Things to keep in mind:**
- **Keep runs occasional.** Running a search once a day is less risky than once
  every hour.
- **You log in by hand.** The first time you run a search, a browser window
  opens and you type your own password and two-factor code into Facebook,
  exactly as you normally would. Your password is never seen or stored by this
  app. Only the resulting browser session is saved on your computer, in a hidden
  folder called `.state`, so you don't have to log in every time.
- **Facebook changes their website constantly.** When they do, searches may
  start coming back empty or with missing prices. Please contact me if this happens so I can update the app accordingly. 

---

## Overview

Every search builds a gallery like this, saved on your computer. You can search
it, sort by price or year, filter by city, and hide anything you don't want
to see again.

![The gallery a run produces](docs/images/gallery.jpg)

---

This is the search setup window.

![The search setup window](docs/images/settings-search.png)

---

Scheduled searches run at an interval you choose, and can be paused, edited or
run on the spot.

![The scheduled searches tab](docs/images/settings-saved.png)

---

When a scheduled search finishes, it emails you a summary.

![An emailed report](docs/images/email-report.png)

---

## Setting it up

You only have to do this once, and it will take about 10 minutes.

### Step 1 — Get the folder onto your computer

On the project's GitHub page, click the green **Code** button, then **Download
ZIP**. Open the downloaded ZIP to unpack it, and move the resulting folder
somewhere you'll find it again, like Documents or Desktop. (If you're on a Mac
and later decide to set up [automated searches](#automated-searches), you may
need to change the folder location, as explained in that section.)

Keep everything in that folder together. The app stores your searches and
results inside it.

### Step 2 — Start it

Open the folder and double-click the file for your computer:

- **Windows:** `Start Faceplace Marketbook (Windows).bat`
- **Mac:** `Start Faceplace Marketbook (Mac).command`

<details>
<summary><strong>If Windows won't open it — "Windows protected your PC"</strong></summary>

Click **More info**, then **Run anyway**. Windows shows that box for anything it
downloaded from outside the Microsoft Store.

</details>
<details>
<summary><strong>If your Mac won't open it — "Apple could not verify ..."</strong></summary>

1. Click Done to dismiss the popup warning.
2. Open **System Settings** → **Privacy & Security**.
3. Scroll down to the **Security** section. You'll see a line saying
   `"Start Faceplace Marketbook (Mac).command" was blocked to protect your Mac`,
   Click **Open Anyway**.
4. Click **Open Anyway** again if asked, and confirm with your password or Touch ID.

**Or run it from Terminal,** which always works and skips Apple's permission
entirely:

1. Press Command-Space, type `Terminal`, and press Return.
2. Drag `Start Faceplace Marketbook (Mac).command` from your folder into the
   Terminal window. It fills in the location for you.
3. Press Return.

If that says `permission denied`, unzipping stripped the file's permission to
run. Type `chmod +x` and a space, drag the file in again, press Return, and then
repeat the steps above.

</details>
<br>

Once you launch the program, a terminal window opens and tells you what it's
doing. One of two things happens next.

**If you already have Python,** it spends a couple of minutes installing what it
needs and downloading the Chromium browser for itself (about 150 MB) then opens
the search window. Every time after that, it'll start up in a second or two.

**If you don't have Python,** it says so and shows you where to get it. Install
it using the instructions below, then double-click the Start file again.

Everything else it installs goes in a `.venv` folder inside the app's folder.
Nothing is changed anywhere else on your computer.

<details>
<summary><strong>Installing Python, if it asks you to</strong></summary>

You need **version 3.9 or newer**.

**On Windows:** go to <https://www.python.org/downloads/windows/> and download
the latest "Windows installer (64-bit)". Run it. On the very first screen,
**tick the box that says "Add python.exe to PATH"** before clicking Install Now.

**On Mac:** go to <https://www.python.org/downloads/macos/> and download the
latest "macOS 64-bit universal2 installer". Open it and click through.

Only install it if the app told you to. Installing a second copy when you
already have one is usually harmless, but it can leave you with several versions
and some confusion about which is in charge.

</details>

### Step 3 — Add a shortcut on your computer, if you like

The Search Setup window offers this the first time it opens. Select where you want it and click **Add shortcut**:

- **Desktop** — an icon called **Faceplace Marketbook** that you double-click to
  start a search, exactly as the Start file does.
- **Dock** (Mac) — keeps it in the Dock permanently. It also puts the app in
  your Applications folder.
- **Start menu** (Windows) — nothing on screen, but typing "faceplace" finds it.

A shortcut is the only thing any of this puts outside the app's folder. Deleting
the shortcut will not delete the app itself.

If you move the app's folder later, the shortcut will still be pointing at where
the folder used to be; the [Troubleshooting](#troubleshooting) section explains how to fix it.

---

## Running a search

Once the app opens, try running a search.

1. Fill out the search setup page. Each setting has a short explanation underneath it to help guide you. Don't worry about creating a Scheduled Search right now — that will be covered later.
2. Click **Start Search**. Facebook will automatically open in a new window.
3. Log into Facebook as you normally would, including any two-factor code or captcha. The app will automatically take you to Marketplace.
4. Dismiss any popups from Facebook. Then, click the location button in the left sidebar and set the radius you want to search around each city. The twelve built-in cities are spaced so that a 500-mile radius will cover the entire continental US; a smaller radius is fine if you only care about listings near the cities you picked.
5. Return to the terminal window and press Enter to start the search.

## While it runs

**The terminal window narrates as it goes:** which city it's on, how many listings
it's found, and how many survived filtering.

**Don't touch the browser window it opens.** The app is driving that window.
Clicking, scrolling, or typing will fight the app for
control, and could cause it to lose its place. You can use your
computer normally otherwise, including your own separate browser — just leave
that one window alone.

**Keep your computer turned on.** The app tries to keep your computer awake while it
works, and it will continue running even if the display turns off to save power. However, closing your laptop lid will put it to sleep, unless there are external displays connected. Keep your computer plugged in with the lid open so that the search doesn't get interrupted.

**You can stop it early.** Press `Control-C` in the terminal to end the search at any time, and it will build the gallery with everything it gathered so far.

---

## Your results

When it finishes, the results gallery will open automatically.

Everything from the run lands in a new folder inside `runs/`, named for your
search and the date, like `runs/my_search_08-05-2026/`. If you manually run the same search
twice in a day, the second becomes `..._1`. In each folder:

- **`gallery.html`** — the browsable catalogue. The photos are baked into this
  one file, so you can move it anywhere and it still works.
- **`lightweight_gallery.html`** — the same catalogue, but it reads the photos
  out of the `thumbnails/` folder instead of carrying them. Better for
  emailing/sharing, but the images won't be visible without the thumbnails
  folder alongside it.
- **`results.csv`** — the same listings as a spreadsheet, for Excel or Numbers.
- **`run.json`** — a record of what was searched and what came back.
- **`thumbnails/`** — the photos as individual files.

In the gallery you can search the text, filter by which city found it, and sort by price, year, or title (A-Z). You can also click
any card to see the full description, or click **View On Facebook** to open the listing in your browser. Hover over a card and click the **✕** in the corner to hide listings
you're not interested in; the app remembers what you've hidden even after you
close and reopen the page.

---

## Automated searches

You can save a search and have the app run it automatically on a fixed interval, to find new listings, or listings that didn't turn up on the first pass. Each time it runs, it will email you a report of what it found.

Each report tells you what's new, what's still there, and what sold or was taken
down since last time.

You need an email account the app can send from. These instructions describe how
to set it up with Gmail, but Outlook, iCloud, etc. all work too. See [using
something other than Gmail](#using-something-other-than-gmail).

### On a Mac, check where this folder lives first

macOS refuses background tasks access to your **Documents**, **Desktop** and
**Downloads** folders. Searches you start yourself are unaffected, but a
scheduled one can't read anything it needs from those three places, so it will
never run.

The fix takes ten seconds: quit the app, open Finder, press **Command-Shift-H**
to go to your home folder, and drag the Faceplace Marketbook folder there. Then
start it again from its new home.

If you'd rather leave the folder where it is, the app will tell you exactly what
to do instead when you turn automatic runs on in Part 3. Windows has no such
restriction.

### Before you start

You need two things: an app password for your email account, and permission for
your computer to wake itself up. Both are one-time setup. Do them in this order.

### Part 1 — Give the app an email password

This is a special password just for this app. Your real Gmail password isn't
used.

![The Email and schedule tab](docs/images/settings-schedule.png)

1. Start the app the normal way (double-click the Start file).
2. Click the **Email & Setup** tab at the top.
3. Leave that window open and go do this in your web browser:
   1. Go to **myaccount.google.com** and sign in.
   2. Click **Security & sign-in** on the left sidebar.
   3. Find **2-Step Verification**. If it's off, turn it on and follow Google's
      steps. You can't create an app password without it.
   4. Back on the Security page, use the search box at the top of the page and
      type **app passwords**. Click "App passwords" in the list of search
      results.
   5. Type a name — **Faceplace Marketbook** is fine — and click **Create**.
   6. Google shows **a sixteen-letter password**. Leave that window open.
4. Back in the app: type your Gmail address in **Your email address**, and those
   sixteen letters in **App password**. Spaces don't matter.
5. Click **Save**, then click **Send a test email**.
6. Check your email. You should have a message from yourself titled "Faceplace
   Marketbook: test message".

If it doesn't arrive, the app will tell you why in the box under the buttons.

**One thing to know about sending mail to yourself:** Gmail sometimes files a
message you sent yourself under **Sent Mail** and never puts it in your inbox.
If the test message isn't in your inbox, look there before assuming it failed.
Pointing a scheduled search at a different address avoids this
entirely.

#### Using something other than Gmail

You don't need a Gmail account. Pick your provider from the **Provider** menu:

- **Outlook / Hotmail / Live** and **iCloud** work the same way, and also need
  an app password created in their own account settings rather than your normal
  one.
- **Other** lets you type in any mail server's address and port, which is the
  route for a work account or your own domain.

### Part 2 — Save a search

1. Click the **New search** tab.
2. Set up your search exactly as you would for a normal run: query, cities,
   price limits, exclusions, etc.
3. Scroll to the bottom, to **Scheduled search**.
4. Give it a name. This becomes the folder name and the subject line of your
   emails.
5. Enter or change the email address you'd like the report to be sent to.
6. Choose how often the search will run.
7. Click **Save scheduled search**.

**Daily searches run every morning.** Searches set in hours run every so many
hours from when the last one started.

> **Don't set up too many, and don't run them too often.** Every run is a full
> sweep of every city you picked, and a lot of automated traffic is what gets
> Facebook accounts limited or banned. A few automated runs per day is usually fine,
> but there's no guarantee.

The **Scheduled searches** tab lists everything you've saved. From there you can
run one immediately, edit it, pause it, or delete it.

**Neither pausing nor deleting touches your results.** Pausing just stops it
running, and you can resume it later. Deleting removes the schedule and the
saved settings; the results folder, the gallery, the photos and everything the
search ever found stay exactly where they are on your computer.

### Part 3 — Let your computer wake itself up

A scheduled search can't run if the computer is asleep and stays asleep. This
part gives it permission to wake up, do the run, and go back to sleep.

1. In the app, go to the **Email & Setup** tab.
2. Click **Turn automatic runs on**. It takes a few seconds, because the app
   then checks that the schedule it just set up can actually reach your files.
3. On a Mac, there will be a prompt asking for your password. This is macOS
   asking, not the app — waking a sleeping Mac on a schedule needs administrator
   rights.
4. Read the message that appears. It tells you if anything is left to do by
   hand.

Then there are a few system settings you might want to change.

<details>
<summary><strong>On a Mac</strong></summary>

Open **System Settings**, click **Battery**, then **Options…** at the bottom.

- **Wake for network access → Always.** This is what lets a sleeping Mac wake up
  for its scheduled run. On *Only on Power Adapter* — the usual default — a
  scheduled run on battery is skipped and happens the next time the machine is
  awake instead, and the report tells you it ran late.
- **Low Power Mode → Never**, or **Only on Battery** if you want it to work
  while unplugged. Keep in mind that automated searches will use up some battery
  life.

**Closing the lid.** With the lid shut, a Mac laptop goes into a deeper sleep,
unless it's connected to an external monitor or display. This makes scheduled
wake-ups unreliable. If you want overnight runs, leave the lid open and let the
screen turn itself off.

</details>
<details>
<summary><strong>On Windows</strong></summary>

**Allow wake timers** Press the Windows key, type **Control Panel**, and open
it. Then go to **Hardware and Sound → Power Options → Change plan settings →
Change advanced power settings**. In the list that appears, expand **Sleep**,
then **Allow wake timers**, set **both** *On battery* and *Plugged in* to
**Enable**, and click **OK**.

The rest are optional:

- **Battery saver**: If it's set to switch on automatically at a high
  percentage, a run scheduled while it's active may be delayed.
- **Closing the lid.** In **Control Panel → Hardware and Sound → Power Options →
  Choose what closing the lid does**, **Sleep** and **Do nothing** both let runs
  happen. **Hibernate** and **Shut down** don't — both stop scheduled runs until
  you turn the computer back on yourself.

</details>
<br>

### What the emails look like

Each report contains:

- How many listings are new, how many are being tracked, and how many sold or
  were taken down.
- Every new listing by name, with a link.
- Every sold or removed listing by name, with its link, so you can see what you
  missed.
- Two attachments: a small gallery of just the new listings, and one of
  everything currently tracked. Photos are included when they fit; on a big
  search the attachments drop photos to stay under email size limits.
  You can always view the full gallery by opening the app on your computer.

You'll also get an email if a search couldn't run:

- **"Please log into Facebook again"** — the saved login expired. Double-click
  **Log into Facebook** in the app's folder, log in, and you're done; the next
  scheduled run works again.
- **"The scheduled run failed"** — something else went wrong. The search stays
  scheduled and tries again next time. The email includes the details.

A report also carries a warning at the top if the run started late, which is
what you'll see if the computer was asleep or switched off at the scheduled
time.

### Where scheduled results live

Scheduled searches write to `runs/saved/<your search name>/` and **rewrite that
same folder every run**, so the folder always holds the current picture rather
than one folder per run. Previous reports are kept in a `history/` folder inside
it.

---

## Keeping it up to date

You don't need to re-download the code when the app changes. Every time you
start it, it checks whether there's a newer version, and prompts you to
update if needed.

---

## Troubleshooting

**"A leftover browser window is still using this app's saved Facebook login."**
A browser window from a previous run never closed. Close any stray Chromium
window and start again.

**It asks me to log into Facebook again.** The saved session expired, or
Facebook wants to re-check. Double-click **Log into Facebook** in the app's
folder and sign in there — signing in with Safari, Chrome or Edge won't help,
because the app keeps its own separate browser login. See [Step
3](#step-3--log-into-facebook).

**One of my cities came back with nothing, or the wrong place.** If the terminal
said Facebook didn't recognise a city, the address used to add it wasn't a real
Marketplace city. Remove it (hover, click the **✕**) and add it again from a
live Marketplace address. See [Adding your own cities](#adding-your-own-cities).

**No email arrived and I never saw an error.** Click **Send a test email** on
the *Email & Setup* tab. A wrong address or password can't be detected until
the mail server is asked, and a scheduled run that can't send has no way to tell
you by email. Your results are still on your computer either way.

**Hardly any results, or none.** Usually the search term is too specific, or the
excluded terms are too broad. Try fewer words. The other likely cause is the
search radius. Run it again and raise the search radius (Step 4 above).

**Some pictures say "image expired".** Facebook's photo links go stale within
hours. The app saves photos as it goes to avoid this, but a few can slip through
on a very long run. Running the search again picks them up.

**A scheduled search never ran.** Work through these in order:

1. Is it paused? Check the *Scheduled searches* tab.
2. Are automatic runs on? Check the *Email & Setup* tab — the dot should be
   green. An orange dot and **on, but blocked** means the schedule exists but
   can't reach your files; the instructions underneath say what to do.
3. Was the computer asleep with the lid shut, hibernating, or switched off? Go
   back through [Part 3](#part-3--let-your-computer-wake-itself-up).
4. If a run started but nothing arrived, the email settings are the likely
   cause. Click **Send a test email**.

**My desktop icon says it can't find the folder, or does nothing.** It holds the
folder's location, so moving or renaming the folder breaks it. Throw the old
icon away, then start the app from the folder's new home — with no shortcut left
anywhere, it offers you a fresh one on that launch. If you'd previously ticked
**Don't ask again**, it won't offer, and the way to ask outright is to run the
Start file from a terminal with `--desktop-icon` on the end.

**It said macOS wouldn't keep the Dock entry.** That happens occasionally; the
Dock is particular about being edited underneath it. The app itself is in your
Applications folder either way, so open that and drag **Faceplace Marketbook**
onto the Dock yourself.

**I moved the app's folder and scheduled runs stopped.** The schedule still
points at the old location. Go to *Email & Setup*, click **Turn off**, then
**Turn on** again. The tab warns about this when it notices.

**A scheduled run happened but no email came.** Look in **Sent Mail** as well as
your inbox. Then click **Send a test email** on the *Email & Setup* tab — it
reports exactly what went wrong.

**"Not starting: a scheduled run has been running since…"** Two runs can't share
one Facebook login, so a manual run won't start while a scheduled one is going.
Wait for it to finish. If you're sure nothing is really running — for instance
the computer lost power partway through a run — the app clears the leftover
marker by itself after 8 hours, or you can delete the file
`.state/schedule/run.lock` in the app's folder.

**A listing I know is sold still shows up.** The app only removes a listing when
it can positively confirm it's gone, because being missing from Facebook's
search results doesn't mean it was taken down — Facebook's rankings shuffle
constantly. So it errs toward keeping things. It re-checks each old listing
about once per interval, and drops it as soon as Facebook says sold or
unavailable.

---

## For the technically inclined

Everything above is the whole manual. If you want the internals — how listings
are extracted, why the scroll loop stops when it does, what the command-line
flags are, and the measurements behind the advice here — see
[docs/how-it-works.md](docs/how-it-works.md).
