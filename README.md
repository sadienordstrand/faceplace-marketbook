# Faceplace Marketbook

Searches Facebook Marketplace across the whole country for something you're
hunting for, then turns the results into a catalogue you can flip through.

Facebook's own search only covers one city at a time and pads the results with
loosely related junk. This searches twelve cities that between them cover the
continental US, throws out the things that don't match, collects each listing's
photo and description, and builds a single web page you can search, sort, and
prune.

Works on Windows and Mac.

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
  session is saved on your computer, in a hidden folder called `.fb_session`, so
  you don't have to log in every time.
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
somewhere you'll find it again, like your Documents folder.

Keep everything in that folder together. The app stores your searches and
results inside it.

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
while. If it ever asks again, just log in again.

### Step 4 — Set your search radius

Once you're logged in, the app loads Marketplace and then **stops and waits for
you** before searching anything. The terminal tells you what to do; the reason
it waits is this:

**Facebook gives every account a 250-mile search radius by default, and this
needs 500.** The radius belongs to your Facebook account, not to this app, so
it's not something the app can set for you. At 250 miles the twelve cities
cover about a quarter of the country, and you'd never know — you'd just get
fewer results and no error.

So in the browser window, click the location control in the left sidebar and
set the distance to **500 miles**. While you're in there, dismiss any Facebook
notification popups. Then come back to the terminal and press **Enter**.

The terminal prints the radius it can see, so you don't have to guess whether
you got it right. It's an account setting and it sticks, so once you've done
this you can press Enter straight past it on future runs.

---

## Running a search

After the login, a **Search Setup** window opens. Fill it in and click **Start
sweep**. Each setting has a short explanation underneath it in the window
itself, so this is only the shape of it:

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

**Leave "Exact matching" off.** When testing with and without "Exact matching" on the same city, 
turning it on found nothing the normal search missed, and it threw away 34 genuine matches.

### Adding your own cities

The twelve cities that come with it are spaced so their 500-mile circles cover
the continental US, so for a nationwide search you don't need to add anything.
Adding cities is for when you want somewhere covered more densely, or you're
searching outside the US.

Facebook identifies a Marketplace city by a short name buried in its web
address. To get it:

1. Open **facebook.com/marketplace** in your ordinary browser.
2. Click the location button near the top left — it shows whichever city you're
   currently browsing.
3. Type the city you want, pick it from the list, and click Apply.
4. Copy the whole web address from the address bar.

Paste that into the box at the bottom of the **Cities** section, give it a name,
and click **Add city**. The app pulls out the part it needs and tells you if the
link isn't one it can use. Cities you add are saved and will be there next time.
To get rid of one, hover over it and click the **✕**, then click again to
confirm.

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
search and the date, like `runs/defender_110_08-05-2026/`. Run the same search
twice in a day and the second becomes `..._1`. Nothing is ever overwritten. In
each folder:

- **`gallery.html`** — the page that opened. The photos are baked into this one
  file, so you can move it, keep it, or email it and it still works.
- **`results.csv`** — the same listings as a spreadsheet, for Excel or Numbers.
- **`run.json`** — a record of what was searched and what came back.
- **`thumbs/`** — the photos as individual files.

In the gallery you can search the text, filter by which city found it, and click
any card for the full description. The **✕** in a card's corner hides listings
you're not interested in; the app remembers what you've hidden even after you
close and reopen the page.

The sort menu offers price, title, and **year**. Year is read out of the
listing's title, which is where vehicle sellers put it — "1995 Land Rover
Defender 110" sorts as 1995. Anything without a year in its title, like a parts
listing, collects at the bottom whichever direction you sort.

---

## When something goes wrong

**"A leftover browser window is still using this app's saved Facebook login."**
A browser window from a previous run never closed. Close any stray Chromium
window and start again.

**It asks me to log into Facebook again.** The saved session expired, or
Facebook wants to re-check. Just log in again.

**Hardly any results, or none.** Usually the search term is too specific, or the
excluded terms are too broad. Try fewer words. The other likely cause is the
search radius: if the app reported less than 500 miles at the pause before the
sweep, the cities no longer cover the country. Run it again and fix the radius
at that pause (Step 4 above).

**Some pictures say "image expired".** Facebook's photo links go stale within
hours. The app saves photos as it goes to avoid this, but a few can slip through
on a very long run. Running the search again picks them up.

---

## For the technically inclined

Everything above is the whole manual. If you want the internals — how listings
are extracted, why the scroll loop stops when it does, what the command-line
flags are, and the measurements behind the advice here — see
[docs/how-it-works.md](docs/how-it-works.md).
