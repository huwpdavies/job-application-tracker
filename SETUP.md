# Setup

## 1. Register the app in Microsoft Entra (one-off, ~5 minutes)

You only do this once; both laptops use the same Client ID.

1. Go to <https://entra.microsoft.com> and sign in with your **Outlook.com** account.
   - Microsoft no longer lets a bare personal account register apps ("The ability to create applications outside of a directory has been deprecated"). You first need a free directory (tenant):
     - If entra.microsoft.com offers to create a directory/tenant, accept (any name). No card needed.
     - Otherwise sign up for a free Azure account at <https://azure.microsoft.com/free> with the same Outlook.com account. It asks for a phone and card for identity verification but isn't charged unless you create paid resources, and this app creates none. This creates a "Default Directory".
   - Then continue with *App registrations* below, inside that directory.
2. **App registrations → New registration**
   - **Name:** `Job Application Tracker`
   - **Supported account types:** *Personal Microsoft accounts only*
     (if you only see "Accounts in any organizational directory and personal Microsoft accounts", that works too).
   - **Redirect URI:** platform **Public client/native (mobile & desktop)**, value `http://localhost`
   - Click **Register**.
3. On the app's **Overview** page, copy **Application (client) ID** → this is `AZURE_CLIENT_ID`.
4. **Authentication** (left menu):
   - Under *Platform configurations* confirm `http://localhost` is listed under *Mobile and desktop applications*.
   - Scroll to **Advanced settings → Allow public client flows** → **Yes** (needed for the device-code fallback). Save.
5. **API permissions → Add a permission → Microsoft Graph → Delegated permissions**, add:
   - `User.Read`
   - `Mail.Read`
   - `Calendars.Read`
   - `offline_access`

   Do **not** add any `.ReadWrite` or `.Send` permission. No admin consent is needed for a personal account; you consent on first sign-in.
6. You do **not** need a client secret. Don't create one.

## 2. Configure `.env`

Open `.env` in the project folder and paste the Client ID:

```
AZURE_CLIENT_ID=<paste Application (client) ID here>
```

Other values: `ANTHROPIC_API_KEY`, `DB_PATH`, `CLAUDE_MODEL`, thresholds. `.env.example` documents them all.

If the project folder is synced by OneDrive, `.env` syncs too. If a setting differs per machine (usually `DB_PATH`, because the OneDrive path differs on macOS), put that line in `.env.local`, which overrides `.env` and is git-ignored.

## 3. Install Python dependencies

Create the virtual environment **outside** the OneDrive folder. A venv is OS-specific and has thousands of files, so syncing it would break the other laptop.

**Windows (PowerShell)**
```powershell
python -m venv $env:LOCALAPPDATA\job-tracker-venv
& $env:LOCALAPPDATA\job-tracker-venv\Scripts\python.exe -m pip install -r requirements.txt
```
Run commands with `& $env:LOCALAPPDATA\job-tracker-venv\Scripts\python.exe -m tracker ...`, or activate it first with `& $env:LOCALAPPDATA\job-tracker-venv\Scripts\Activate.ps1`.

**macOS (Terminal)**
```bash
python3 -m venv ~/.job-tracker-venv
~/.job-tracker-venv/bin/python -m pip install -r requirements.txt
source ~/.job-tracker-venv/bin/activate
```

## 4. Sign in and test

```
python -m tracker login          # opens your browser; sign in and approve read-only access
python -m tracker list-recent    # prints your 10 most recent emails
python -m tracker find-folder    # checks the "Job Applications" folder can be found
```

If the browser pop-up doesn't work, use `python -m tracker login --device-code`.

Then start the dashboard (the first full email scan, which asks you to confirm a cost estimate, is best done in the terminal with `python -m tracker sync`):

```
python -m tracker serve
```

See [README.md](README.md) for everything else, including how to run the tests (`python -m pytest`).

Each machine signs in separately. The sign-in token is stored in a per-machine folder (Windows: `%LOCALAPPDATA%\JobApplicationTracker`, macOS: `~/Library/Application Support/JobApplicationTracker`), never in the project or database folder.

## Troubleshooting

- **AADSTS700016 / "application not found"**: wrong Client ID, or the account type isn't set to include personal accounts.
- **AADSTS50011 redirect URI mismatch**: the redirect URI must be the *Public client/native* platform with `http://localhost` (not *Web*, not `https`).
- **AADSTS65001 consent required**: sign in again and click *Accept* on the permissions screen.
