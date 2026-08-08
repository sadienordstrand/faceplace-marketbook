# Faceplace Marketbook

Searches Facebook Marketplace across the whole country for something you're
hunting for, then turns the results into a catalogue you can flip through.

Facebook's own search only covers one city at a time and pads the results with
loosely related junk. This searches twelve cities that between them cover the
continental US, throws out the things that don't match, collects each listing's
photo and description, and builds a single web page you can search, sort, and
prune.

You can also [save a search and set it up to run automatically](#automated-searches)
on a schedule, and have it email you what's new — and what sold — since last time.

Works on Windows and Mac.

## Contents

- [**Results**](#results) — what a finished search looks like
- [**Please read this first**](#please-read-this-first) — this tool uses your real
  Facebook account
- [**Setting it up**](#setting-it-up) — about ten minutes, once
  - [Step 1 — Get the folder onto your computer](#step-1--get-the-folder-onto-your-computer)
  - [Step 2 — Start it](#step-2--start-it)
  - [Step 3 — Log into Facebook](#step-3--log-into-facebook)
  - [Step 4 — Set your search radius](#step-4--set-your-search-radius)
  - [Step 5 — Put it on your desktop, if you like](#step-5--put-it-on-your-desktop-if-you-like)
- [**Running a search**](#running-a-search) — the settings window, and how to get
  cleaner results
  - [Getting better results](#getting-better-results)
  - [Adding your own cities](#adding-your-own-cities)
- [**While it runs**](#while-it-runs) — what to expect, and what not to touch
- [**Your results**](#your-results) — the gallery and the files beside it
- [**Automated searches**](#automated-searches) — run on a schedule and get
  emailed what's new
  - [On a Mac, check where this folder lives first](#on-a-mac-check-where-this-folder-lives-first)
  - [Part 1 — Give the app an email password](#part-1--give-the-app-an-email-password)
  - [Part 2 — Save a search](#part-2--save-a-search)
  - [Part 3 — Let your computer wake itself up](#part-3--let-your-computer-wake-itself-up)
  - [What the emails look like](#what-the-emails-look-like)
  - [Where scheduled results live](#where-scheduled-results-live)
  - [Turning it off](#turning-it-off)
- [**When something goes wrong**](#when-something-goes-wrong) — the usual
  problems, and what to do
- [**For the technically inclined**](#for-the-technically-inclined) — how it
  works inside

---

## Results

Every run builds a gallery like this one, saved on your own computer. You can
search it, sort it by price or year, filter by city, and throw out anything you
don't want to see again.

![The gallery a run produces](docs/images/gallery.jpg)

Each listing keeps its photo, price, location, mileage where the seller gave one,
and the opening lines of the description, with a link straight to the listing on
Facebook.

<img src="docs/images/gallery-card.jpg" alt="A single listing card" width="300">

Searches are set up in a window like this. Every setting has
an explanation underneath it.

![The search setup window](docs/images/settings-search.png)

Saved searches run on a schedule you choose, and can be paused, edited or run on
the spot.

![The saved searches tab](docs/images/settings-saved.png)

When a scheduled search finishes, it emails you a summary of what's new and what
has sold or been taken down since last time.

![An emailed report](docs/images/email-report.png)

---

## Please read this first

**Automating Facebook is against their Terms of Service.** This runs on your own
real Facebook account, using your own login. There's no trickery here to hide
that — deliberately. The practical consequences:

- **Keep runs occasional.** A search every few days is very different from one
  every hour. Facebook can restrict or disable accounts for automated activity,
  and it would be your account.
- **Collecting descriptions is the heavy part.** It opens one page per listing,
  which is far more activity than the search itself. If you're at all worried,
  set the pace to "Slow" and use the Limit box.
- **You log in by hand.** A browser window opens and you type your own password
  and two-factor code into Facebook, exactly as you normally would. Your
  password is never seen or stored by this app. Only the resulting browser
  session is saved on your computer, in a hidden folder called `.state`, so you
  don't have to log in every time.
- **Facebook changes their website constantly.** When they do, searches may
  start coming back empty or with missing prices. That's expected wear, not
  something you broke.

---

## Setting it up

You only do this once. Budget about ten minutes, most of it waiting for
downloads.

### Step 1 — Get the folder onto your computer

On the project's GitHub page, click the green **Code** button, then **Download
ZIP**. Open the downloaded ZIP to unpack it, and move the resulting folder
somewhere you'll find it again.

Keep everything in that folder together. The app stores your searches and
results inside it. Anywhere you'll find it again is fine — Documents works, so
does the desktop. (If you later set up [automated searches](#automated-searches)
on a Mac, there's one folder location that needs changing, and that section says
so when you get there.)

### Step 2 — Start it

Open the folder and double-click the file for your computer:

- **Windows:** `Start Faceplace (Windows).bat`
- **Mac:** `Start Faceplace (Mac).command`

A terminal window opens and tells you what it's doing. One of two things happens
next.

**If you already have Python,** it spends a couple of minutes installing what it
needs and downloading the Chromium browser for itself, about 150 MB, then opens
the search window. Every time after that, it'll start up in a second or two.

**If you don't have Python,** it says so and shows you where to get it.
Install it using the instructions below, then double-click the Start file
again.

Everything else it installs goes in a `.venv` folder inside the app's folder.
Nothing is changed anywhere else on your computer, and deleting the app's folder
removes all of it.

<details>
<summary><strong>Installing Python, if it asks you to</strong></summary>

You need **version 3.9 or newer**. If you're installing fresh, just take the
latest.

**On Windows:** go to <https://www.python.org/downloads/windows/> and download
the latest "Windows installer (64-bit)". Run it. On the very first screen,
**tick the box that says "Add python.exe to PATH"** before clicking Install Now.
That box matters — without it the app can't find Python.

**On Mac:** go to <https://www.python.org/downloads/macos/> and download the
latest "macOS 64-bit universal2 installer". Open it and click through.

Only install it if the app told you to. Installing a second copy when you
already have one is usually harmless, but it can leave you with several versions
and some confusion about which is in charge.

</details>

<details>
<summary><strong>If your computer won't open the Start file</strong></summary>

Both systems block programs that aren't from an identified company, and this one
isn't.

**Windows** shows a blue "Windows protected your PC" box. Click **More info**,
then **Run anyway**.

**Mac** shows *"Apple could not verify ... is free of malware"*.
The button you need is in Settings:

1. Open **System Settings** → **Privacy & Security**.
2. Scroll down to the **Security** section. You'll see a line saying
   `"Start Faceplace (Mac).command" was blocked to protect your Mac`, with an
   **Open Anyway** button next to it. Click it.
3. Confirm with your password or Touch ID, and click **Open Anyway** again if
   asked.

**Mac, alternative that always works:** you can run it from Terminal instead and
skip Apple's permission entirely.

1. Press Command-Space, type `Terminal`, and press Return.
2. Drag `Start Faceplace (Mac).command` from your folder into the Terminal
   window. It fills in the location for you.
3. Press Return.

If that says `permission denied`, unzipping stripped the file's permission to
run. Type `chmod +x` and a space, drag the file in again, press Return, and then
repeat the steps above.

</details>

### Step 3 — Log into Facebook

The first run opens a Facebook window and waits. Log in as you normally would,
including any two-factor code or captcha. The app notices when you're through
and carries on by itself.

It remembers the session, so you shouldn't have to do this again for a long
while — usually a few weeks, until Facebook expires it.

**When it does expire, log in through the app, not your normal browser.** The app
keeps its own private browser login, separate from Safari, Chrome and Edge, so
signing into Facebook the usual way has no effect on it. You have two ways to
renew it, and neither needs the command line:

- Double-click **Log into Facebook** (`Log into Facebook (Mac).command` or
  `Log into Facebook (Windows).bat`). A Facebook window opens, you log in, and it
  saves the session and closes. Nothing gets searched. This is the one to use
  when a scheduled run emails you saying the login expired.
- Or just start a search as usual. If the login has expired you'll be asked for it
  before the sweep begins, exactly as you were the first time.

### Step 4 — Set your search radius

Once you're logged in, the app loads Marketplace and then **stops and waits for
you** before searching anything.

In the browser window, click the location control in the left sidebar and
set the distance to **500 miles**. While you're in there, dismiss any Facebook
notification popups. Then come back to the terminal and press **Enter**.

### Step 5 — Put it on your desktop, if you like

The Search Setup window offers this by itself the first time it opens, with a
small panel headed **Add a shortcut?** Tick where you want it and click **Add
shortcut**:

- **Desktop** — an icon called **Faceplace Marketbook** that you double-click to
  start a search, exactly as the Start file does.
- **Dock** (Mac) — keeps it in the Dock permanently. It also puts the app in your
  own Applications folder, which is what the Dock points at, and the Dock blinks
  as it restarts.
- **Start menu** (Windows) — nothing on screen, but typing "Faceplace" finds it.

If you'd rather not, click **Not now** and it'll ask again next time. Tick
**Don't ask again** first and it won't. Either way it stops asking once you have
a shortcut, and it never asks on a computer that already has one.

You can also do it whenever you like, without waiting to be asked. In the Search
Setup window, open the **Email & schedule** tab and click **Add a shortcut…**.
That works even if you ticked **Don't ask again**, and even if you already have
one somewhere and want a second.

A shortcut is the only thing any of this puts outside the app's folder. Dragging
it to the Trash (or deleting it, on Windows) removes it and nothing else.

If you move the app's folder later, the icon will still be pointing at where the
folder used to be. Click **Add a shortcut…** again from its new home and you'll
get one that works.

---

## Running a search

After the login, a **Search Setup** window opens. It has three tabs across the
top — **New search** is the one you want now; the other two are for
[automated searches](#automated-searches).

Fill in the New search tab and click **Start sweep**. Each setting has a short
explanation underneath it in the window itself; this is just a brief overview:

- **Query** — what you're looking for, e.g. `land rover defender 110`. More
  words narrows the results.
- **Cities** — each selected city searches a 500-mile radius around itself.
  Select all of them to cover the continental US. You can add your own cities
  too; see below.
- **Price range** and **Exclude terms** — the two most effective ways to cut
  junk. See below.
- **Stages** — whether to collect descriptions, download photos, and build the
  gallery. All three on is the normal choice.
- **Description retrieval** — the pace, and an optional cap on how many
  listings get a description.

The footer shows a running estimate of how long each listing will take, so you
can see the cost of your choices before committing.

### Getting better results

Facebook search is a suggestion, not a filter. It pads results heavily: in one
measured search, **85% of what came back didn't have the search term in the
title at all.** Two settings do most of the work of cleaning that up:

**Exclude terms** is the strongest one, especially when your search term is also
some other product's name. Searching "defender 110" returns thousands of Can-Am
UTVs, whose model names are things like HD10. Adding `can am, hd10, hot wheels`
cut one real search from 4,698 listings to 1,925. Punctuation and spacing are
ignored, so `can am` also catches "Can-Am" and "CANAM".

**A minimum price** can also narrow the results considerably: it took that same
search from 1,925 listings down to 811. Listings with no price shown are kept
either way.

**Leave "Exact matching" off.** When testing with and without "Exact matching"
on the same city, turning it on found nothing the normal search missed, and it
threw away 34 genuine matches.

### Adding your own cities

The twelve cities that come with it are spaced so their 500-mile circles cover
the continental US, so for a nationwide search you don't need to add anything.
But if you want to add your own city to search, you can do that.

Facebook identifies a Marketplace city by a string buried in its web
address. To get it:

1. Open **facebook.com/marketplace** in your ordinary browser.
2. Click the location, type the city you want, pick it from the dropdown, and
   click Apply.
3. Copy the whole web address from the address bar.

Paste that into the box at the bottom of the **Cities** section, give it a name
(like "City, ST") and click **Add city**. The app pulls out the part it needs and
tells you if the link isn't one it can use. Cities you add are saved and will be
there next time. To get rid of one, hover over it and click the **✕**, then click
again to confirm.

**Only cities you added yourself can be removed.** The twelve that come with the
app are permanent, because their spacing is what makes nationwide coverage work
and removing one leaves a hole nothing would show you afterwards. To leave one
out of a search, untick it — that has exactly the effect you want and it's not
permanent.

Your cities are kept in their own file, `.state/my_locations.json`, separate from
the twelve in `src/locations.json`. Nothing you do in the window ever changes
that second file, so an update to the app can't lose your cities and adding one
can't damage the built-in list.

**If the address you paste isn't really a city, you won't find out until a run.**
The app checks that the link has a city in it, but it can't tell a real city from
a plausible-looking one — only Facebook knows that. Paste something Facebook
doesn't recognise and it quietly answers with results for *whatever city your
Facebook account is currently set to*, which would file another city's listings
under your new name. So the app watches for that during the run: if it happens,
the city is skipped, the terminal says which city Facebook substituted, and a
scheduled run puts a warning at the top of its email. Remove the bad entry and
add it again from a real Marketplace address.

---

## While it runs

The terminal window narrates as it goes: which city it's on, how many listings
it's found, and how many survived filtering. It's meant to be readable — if it
looks stuck, check the last line.

**Don't touch the browser window it opens.** The app is driving that window —
scrolling it, reading it, and moving between pages. Clicking, scrolling, typing,
or opening a listing in it will fight the app for control, and at worst it loses
the city it was working on. Leave it alone and let it work. You can use your
computer normally otherwise, including your own separate browser — just leave
that one window be.

Collecting descriptions is the slow part, roughly 7 seconds per listing, because
each one is a separate page visit and the app deliberately pauses between them
to avoid getting flagged for suspicious bot activity. A few thousand listings is
an overnight job. The estimate is printed before that stage starts.

**Sleep is handled for you.** A long run would normally be cut short by the
computer going to sleep, so the app asks your system to stay awake while it
works, and releases that as soon as it's done. It says so in the terminal.
One thing it can't override: **closing a laptop lid still puts the machine to
sleep.** For an overnight run, leave the lid open and leave it plugged in.

**You can stop it early.** Press `Control-C` in the terminal while it's
collecting descriptions and it stops, keeps everything gathered so far, and goes
straight to building your gallery.

---

## Your results

When it finishes, your gallery opens in your browser automatically.

Everything from the run lands in a new folder inside `runs/`, named for your
search and the date, like `runs/my_search_08-05-2026/`. Run the same search
twice in a day and the second becomes `..._1`. Nothing is ever overwritten. In
each folder:

- **`gallery.html`** — the page that opened. The photos are baked into this one
  file, so you can move it, keep it, or email it and it still works.
- **`results.csv`** — the same listings as a spreadsheet, for Excel or Numbers.
- **`run.json`** — a record of what was searched and what came back.
- **`thumbnails/`** — the photos as individual files.

In the gallery you can search the text, filter by which city found it, and click
any card for the full description. The **✕** in a card's corner hides listings
you're not interested in; the app remembers what you've hidden even after you
close and reopen the page.

The sort menu offers price, title, and **year**. Year is read out of the
listing's title, which is where vehicle sellers put it — "1995 Land Rover
Defender 110" sorts as 1995. Anything without a year in its title, like a parts
listing, collects at the bottom whichever direction you sort.

---

## Automated searches

You can save a search under a name and have the app run it on its own — every
day, every few hours, whatever you pick — and email you what turned up. It only
looks up descriptions and photos for listings it has never seen before, so after
the first run these are much quicker.

Each report tells you what's new, what's still there, and what sold or was
taken down since last time.

You need an email account the app can send from. Gmail is what these instructions
use because it's the most common and the fiddliest; Outlook, Hotmail, Live,
iCloud and any other server you know the address of all work too. See
[using something other than Gmail](#using-something-other-than-gmail).

### On a Mac, check where this folder lives first

macOS refuses background tasks access to your **Documents**, **Desktop** and
**Downloads** folders. Searches you start yourself are unaffected, but a
scheduled one can't read anything it needs from those three places, so it will
never run.

The fix takes ten seconds: quit the app, open Finder, press **Command-Shift-H**
to go to your home folder, and drag the Faceplace Marketbook folder there. Then
start it again from its new home. Nothing is lost by moving it — your searches,
results and Facebook login all live inside the folder and travel with it.

If you'd rather leave the folder where it is, the app will tell you exactly what
to do instead when you turn automatic runs on in Part 3. Windows has no such
restriction.

### Before you start

You need two things: an app password for your email account, and permission for
your computer to wake itself up. Both are one-time setup. Do them in this order.

### Part 1 — Give the app an email password

This is a special password just for this app. Your real Gmail password isn't used.

![The Email and schedule tab](docs/images/settings-schedule.png)

1. Start the app the normal way (double-click the Start file).
2. Click the **Email & schedule** tab at the top.
3. Leave that window open and go do this in your web browser:
   1. Go to **myaccount.google.com** and sign in.
   2. Click **Security & sign-in** on the left sidebar.
   3. Find **2-Step Verification**. If it's off, turn it on and follow Google's
      steps. You can't create an app password without it.
   4. Back on the Security page, use the search box at the top of the page and
      type **app passwords**. Click "App passwords" in the list of search results.
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
Having reports sent to a different address avoids this entirely.

#### If the address or password is wrong

The app checks the *shape* of what you typed the moment you click **Save**: a
missing @, a space in the middle, a password that isn't the sixteen lowercase
letters Google issues. Those it tells you about immediately, and a mistyped
address isn't saved at all.

What it can't know without asking the mail server is whether the address and
password are *the right ones*. That's what **Send a test email** is for, and it's
worth the ten seconds — without it, the first sign of a wrong password is a
scheduled run at 5am that finishes, writes all its results, and can't send them.
Nothing is lost when that happens (the gallery is still on your computer, and the
next run picks up where it left off) but you won't hear about it by email, for the
obvious reason.

One quirk to know, because it sends people looking in the wrong place: if the
**address** is wrong, the mail server rejects the login and reports it as a bad
password. The app says so in its message, but if a password you're sure about is
being refused, check the address for a typo too.

#### Using something other than Gmail

You don't need a Gmail account. Pick your provider from the **Provider** menu:

- **Outlook / Hotmail / Live** and **iCloud** work the same way, and also need an
  app password created in their own account settings rather than your normal one.
- **Other** lets you type in any mail server's address and port, which is the
  route for a work account or your own domain.

Two things hold whichever you choose. The address you enter has to be the mailbox
you're logging into, because mail servers won't let you send as somebody else.
The address reports are *sent to* can be anything at all — a different account, a
partner, a phone-number-to-text gateway. The Gmail sent-to-yourself quirk above
only applies to Gmail.

### Part 2 — Save a search

1. Click the **New search** tab.
2. Set up your search exactly as you would for a normal run: query, cities, price
   limits, exclusions.
3. Scroll to the bottom, to **Run this on a schedule**.
4. Give it a name. This becomes the folder name and the subject line of your
   emails, so make it something you'll recognize.
5. Choose how often, using the number box and the **Hours / Days** menu.
6. Click **Save scheduled search**.

**Daily searches run every morning.** Searches set in hours run every so many hours
from when the last one started.

> **Don't set up too many, and don't run them too often.** Every run is a full
> sweep of every city you picked, and a lot of automated traffic is what gets
> Facebook accounts limited or banned. **Once a day is usually plenty, 
> and more often than every 6 hours is asking for trouble. Two or
> three saved searches is a sensible ceiling. The app warns you when you go past
> these, but it won't stop you — it's your account.

The **Saved searches** tab lists everything you've saved. From there you can run
one immediately, edit it, pause it, or delete it.

**Neither pausing nor deleting touches your results.** Pausing just stops it
running, and you can resume it later. Deleting removes the schedule and the saved
settings; the results folder, the gallery, the photos and everything the search
ever found stay exactly where they are on your computer, and every listing it
ever collected is still in the app's database.

The one thing deleting does lose is the search's memory of what it had already
seen. If you save a new search with the same name later, it writes to the same
results folder again, but its first report will call everything new, because it
has nothing to compare against any more.

### Part 3 — Let your computer wake itself up

A scheduled search can't run if the computer is asleep and stays asleep. This
part gives it permission to wake up, do the run, and go back to sleep.

1. In the app, go to the **Email & schedule** tab.
2. Click **Turn automatic runs on**. It takes a few seconds, because the app then
   checks that the schedule it just set up can actually reach your files.
3. On a Mac, there will be a prompt asking for your password. This is
   macOS asking, not the app — waking a sleeping Mac on a schedule needs
   administrator rights.
4. Read the message that appears. It tells you if anything is left to do by hand.

Then there are a few system settings you might want to change.

#### On a Mac

Open **System Settings**, click **Battery**, then **Options…** at the bottom.

- **Wake for network access → Always.** This is what lets a sleeping
  Mac wake up for its scheduled run. On *Only on Power Adapter* — the usual
  default — a scheduled run on battery is skipped and happens the next time the
  machine is awake instead, and the report tells you it ran late. Nothing is
  lost either way.
- **Low Power Mode → Never**, or **Only on Battery** if you want it to work while
  unplugged. Keep in mind that automated searches will use up some battery life.

**Closing the lid.** With the lid shut and no display attached, a Mac laptop goes
into a deeper sleep, and scheduled wake-ups stop being dependable. If you
want overnight runs, leave the lid open and let the screen turn itself off.

**With external monitors, a shut lid is fine.** A Mac driving an external display
stays in the ordinary kind of sleep, so it wakes for a run normally.

#### On Windows

**Allow wake timers** Press the Windows key, type **Control Panel**, and open it. Then go to 
**Hardware and Sound → Power Options → Change plan settings → Change advanced power settings**.
In the list that appears, expand **Sleep**, then **Allow wake timers**, set
**both** *On battery* and *Plugged in* to **Enable**, and click **OK**.

The rest are optional:

- **Battery saver**: If it's set to switch on automatically at a high percentage, a
  run scheduled while it's active may be delayed.
- **Closing the lid.** In **Control Panel → Hardware and Sound → Power Options →
  Choose what closing the lid does**, **Sleep** and **Do nothing** both let runs
  happen. **Hibernate** and **Shut down** don't — both stop scheduled runs until
  you turn the computer back on yourself.

### What the emails look like

Each report contains:

- How many listings are new, how many are being tracked, and how many sold or
  were taken down.
- Every new listing by name, with a link.
- Every sold or removed listing by name, with its link, so you can see what you
  missed.
- Two attachments: a small gallery of just the new listings, and one of
  everything currently tracked. Photos are included when they fit; on a big
  search the attachments drop photos to stay under email size limits, and the
  message tells you where the full gallery is on your computer.

You'll also get an email if a run couldn't happen:

- **"Please log into Facebook again"** — the saved login expired. Double-click
  **Log into Facebook** in the app's folder, log in, and you're done; the next
  scheduled run works again. Logging into Facebook in your normal browser won't
  fix it, because the app keeps its own separate login.
- **"The scheduled run failed"** — something else went wrong. The search stays
  scheduled and tries again next time. The email includes the details.

A report also carries a warning at the top if the run started late, which is what
you'll see if the computer was asleep or switched off at the scheduled time.
Nothing is lost — it runs as soon as the machine is available again.

### Where scheduled results live

Scheduled searches write to `runs/saved/<your search name>/` and **rewrite that
same folder every run**, so the folder always holds the current picture rather
than one folder per run. Previous reports are kept in a `history/` folder inside
it.

### Turning it off

- **One search:** *Saved searches* tab → **Pause**. Or **Delete** to remove it;
  its results folder, gallery and photos all stay on your computer.
- **All of it:** *Email & schedule* tab → **Turn them off**. Your saved searches
  stay, they just stop running by themselves.

Nothing you can click in this app deletes results. If you want the disk space
back, delete the folders inside `runs/` yourself.

---

## When something goes wrong

**"A leftover browser window is still using this app's saved Facebook login."**
A browser window from a previous run never closed. Close any stray Chromium
window and start again.

**It asks me to log into Facebook again.** The saved session expired, or Facebook
wants to re-check. Double-click **Log into Facebook** in the app's folder and sign
in there — signing in with Safari, Chrome or Edge won't help, because the app
keeps its own separate browser login. See
[Step 3](#step-3--log-into-facebook).

**One of my cities came back with nothing, or the wrong place.** If the terminal
said Facebook didn't recognise a city, the address used to add it wasn't a real
Marketplace city. Remove it (hover, click the **✕**) and add it again from a live
Marketplace address. See [Adding your own cities](#adding-your-own-cities).

**I can't remove one of the built-in cities.** That's deliberate — untick it
instead. Only cities you added yourself can be removed.

**No email arrived and I never saw an error.** Click **Send a test email** on the
*Email & schedule* tab. A wrong address or password can't be detected until the
mail server is asked, and a scheduled run that can't send has no way to tell you
by email. Your results are still on your computer either way.

**Hardly any results, or none.** Usually the search term is too specific, or the
excluded terms are too broad. Try fewer words. The other likely cause is the
search radius: if the app reported less than 500 miles at the pause before the
sweep, the cities no longer cover the country. Run it again and fix the radius
at that pause (Step 4 above).

**Some pictures say "image expired".** Facebook's photo links go stale within
hours. The app saves photos as it goes to avoid this, but a few can slip through
on a very long run. Running the search again picks them up.

**A scheduled search never ran.** Work through these in order:

1. Is it paused? Check the *Saved searches* tab.
2. Are automatic runs on? Check the *Email & schedule* tab — the dot should be
   green. An orange dot and **on, but blocked** means the schedule exists but
   can't reach your files; the instructions underneath say what to do.
3. Was the computer asleep with the lid shut, hibernating, or switched off? Go
   back through [Part 3](#part-3--let-your-computer-wake-itself-up).
4. If a run started but nothing arrived, the email settings are the likely cause.
   Click **Send a test email**.

**My desktop icon says it can't find the folder, or does nothing.** It holds the
folder's location, so moving or renaming the folder breaks it. Start the app from
its folder, then click **Add a shortcut…** on the *Email & schedule* tab — see
[Step 5](#step-5--put-it-on-your-desktop-if-you-like).

**I said "don't ask again" and now I want a shortcut.** Click **Add a shortcut…**
on the *Email & schedule* tab. Saying that stops it asking; it doesn't stop you
having one.

**It said macOS wouldn't keep the Dock entry.** That happens occasionally; the
Dock is particular about being edited underneath it. The app itself is in your
Applications folder either way, so open that and drag **Faceplace Marketbook**
onto the Dock yourself.

**I moved the app's folder and scheduled runs stopped.** The schedule still
points at the old location. Go to *Email & schedule*, click **Turn them off**,
then **Turn automatic runs on** again. The tab warns about this when it notices.

**A scheduled run happened but no email came.** Look in **Sent Mail** as well as
your inbox. Then click **Send a test email** on the *Email & schedule* tab — it
reports exactly what went wrong.

**"Not starting: a scheduled run has been running since…"** Two runs can't share
one Facebook login, so a manual run won't start while a scheduled one is going.
Wait for it to finish. If you're sure nothing is really running — for instance the
computer lost power partway through a run — the app clears the leftover marker by
itself after 8 hours, or you can delete the file `.state/schedule/run.lock` in the
app's folder.

**A listing I know is sold still shows up.** The app only removes a listing when
it can positively confirm it's gone, because being missing from Facebook's search
results doesn't mean it was taken down — Facebook's rankings shuffle constantly.
So it errs toward keeping things. It re-checks each old listing about once per
interval, and drops it as soon as Facebook says sold or unavailable.

---

## For the technically inclined

Everything above is the whole manual. If you want the internals — how listings
are extracted, why the scroll loop stops when it does, what the command-line
flags are, and the measurements behind the advice here — see
[docs/how-it-works.md](docs/how-it-works.md).
