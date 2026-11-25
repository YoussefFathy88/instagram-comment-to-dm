# Comment to DM for Instagram

Someone comments a keyword on your reel (for example **"AI"**). This tool then:

1. **Sends them a DM** with your link (a private reply to their comment).
2. **Replies under their comment** ("Check your DMs 📩"), with a different text each time.
3. **Tracks everything** on a small dashboard on your PC: who got the DM, who didn't, and why.

It runs on your own computer, with your own Instagram app. No paid tools, no third-party service.


---

## What you need

- **Windows** PC (the hourly runs use Windows Task Scheduler. The rest is plain Python.)
- **Python 3.14** ([python.org](https://www.python.org/downloads/))
- An **Instagram Professional account** (Creator or Business), public
- A free **Meta developer account** ([developers.facebook.com](https://developers.facebook.com))

Setup takes about **30-45 minutes** once.

---

## Step 1: Download and install

```powershell
git clone https://github.com/YoussefFathy88/instagram-comment-to-dm.git
cd instagram-comment-to-dm\backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy ..\.env.example ..\.env
```

`.env` holds your token. **Never share it or upload it.** (It is already in `.gitignore`.)

## Step 2: Set up Instagram

1. Instagram → Settings → **Messages and story replies** → **Message requests**: keep **Everyone**.
2. Make sure the account is **Professional** and **public**.

## Step 3: Create your Meta app

Meta renames buttons often. If a label is slightly different, pick the closest one.

1. [developers.facebook.com/apps](https://developers.facebook.com/apps) → **Create App**.
2. Use case: **Manage messaging & content on Instagram** (look under "Business messaging" or "All").
3. Pick your business portfolio (an unverified one is fine) → **Create app**.
4. Open the use case → **API setup with Instagram login**:
   - **Add all required permissions**: `instagram_business_basic`, `instagram_business_manage_comments`, `instagram_business_manage_messages`.
   - Left menu → **App roles** → **Add People** → **Instagram Tester** → your Instagram account (add a second account too, for testing).
   - Accept each invite on **instagram.com/accounts/manage_access/** → **Tester invites** (the phone app hides this page).
   - Back on the setup page → **Add account** → log in → **Generate token**.
5. Paste the token into `.env` as `INSTAGRAM_ACCESS_TOKEN=`. Set `TIMEZONE=` to your timezone (e.g. `Africa/Cairo`).
6. On your phone: Instagram → Settings → Messages and story replies → Message controls → **Connected tools** → turn on **Allow access to messages**.
7. Check it works (read-only, sends nothing):

   ```powershell
   .venv\Scripts\python -m app.cli setup-instagram --write
   ```
   It shows your username and saves your `INSTAGRAM_USER_ID`.

## Step 4: Publish the app (important)

While the app is **unpublished**, Instagram hides comments from normal followers (you'll see 0 comments).

1. You need a public **privacy policy** page. Start from [`legal/privacy.example.html`](legal/privacy.example.html), replace every [BRACKET] with your own details, and host it anywhere public (GitHub Pages works).
2. **App settings → Basic**: Privacy policy URL = your link. Data deletion instructions URL = the same link. Category = Business. **Save**.
3. Left menu → **Publish**.

For your **own** account you don't need App Review or business verification.

## Step 5: Your first campaign

A campaign = one reel + keywords + your DM text.

```powershell
.venv\Scripts\python -m app.cli engage-media
copy ..\campaigns\example.yaml ..\campaigns\my-reel.yaml
```

Open `campaigns\my-reel.yaml` and set the `name`, the reel link, the keywords and your DM message (with your link). Then:

```powershell
.venv\Scripts\python -m app.cli engage-save ..\campaigns\my-reel.yaml
.venv\Scripts\python -m app.cli engage-run --campaign my-reel --dry-run
```

The **dry run** shows who would get what. It sends nothing. When it looks right:

```powershell
.venv\Scripts\python -m app.cli engage-approve my-reel
.venv\Scripts\python -m app.cli engage-run --campaign my-reel --max 5
```

`--max 5` sends to 5 people first, so you can check the DMs before the rest.

## Step 6: Dashboard and auto-run

```powershell
..\scripts\dashboard.cmd
```
Opens the control board at **http://127.0.0.1:8765**: send a batch, dry run, pause, turn the scheduler on or off, and see the status of every comment.

To answer new comments every hour while your PC is on:

```powershell
.venv\Scripts\python -m app.cli engage-schedule --every 60
```
Turn it off with `engage-schedule --off`. Optional, start the board when you log in: `board-autostart --on`.

---

## Good to know

- **One DM per comment, within 7 days** of the comment (Instagram's rule). Max 1000 bytes (about 450 Arabic letters).
- **Safe by default:** a new campaign only does dry runs until you approve it. Editing the campaign drops the approval.
- **Rate limits:** each run is capped well under Meta's limits (170 calls, 50 people). Don't raise them.
- **Token:** lasts 60 days. The hourly run renews it every week. If it expires, generate a new one (Step 3.4).
- **Privacy:** the local database and reports hold commenters' usernames. Keep them on your PC. If someone asks to be removed: `engage-forget <username>`.
- **Local only:** the dashboard has no login and only listens on your own PC (127.0.0.1). Don't expose it to the internet.
- Other commands: `engage-status`, `engage-report --sync`, `engage-pause`, `engage-refresh-token`. Run `.venv\Scripts\python -m app.cli --help` for all.

## Run the tests

```powershell
cd backend
.venv\Scripts\python -m pytest
```

## License

MIT. See [LICENSE](LICENSE). Use at your own risk, and follow Instagram's and Meta's terms.
