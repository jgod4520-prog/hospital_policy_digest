# Federal Hospital Policy Digest

A Python script that emails you a weekly digest of federal hospital policy news, filtered and summarized using the Anthropic Claude API.

## What it does

1. **Collects articles** from the past 7 days via RSS feeds: Politico Healthcare, STAT News, KFF Health News, Roll Call (health care), Becker's Hospital Review

2. **Filters for relevance** using `claude-sonnet-4-6`: keeps only articles where (a) the federal government is the primary actor and (b) hospitals are the primary subject. Relevant topics include Medicare/Medicaid hospital payment policy (IPPS, OPPS, CAH, DSH), the Rural Health Transformation Program, site-neutral payments, hospital price transparency, CMS rulemaking targeting hospitals, congressional legislation primarily affecting hospitals, and federal prior authorization reform.

3. **Summarizes** each relevant article in 2–3 sentences and groups them by theme (Payment Policy, Rural Health, CMS Rulemaking, Legislation, Price Transparency).

4. **Emails the digest** as a formatted HTML email via Gmail SMTP.

## Prerequisites

- Python 3.10+
- A Gmail account with **2-Step Verification enabled** and a **Gmail App Password**
- An [Anthropic API key](https://console.anthropic.com/)

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/policy-digest.git
cd policy-digest
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv

# Activate — Windows (PowerShell):
.venv\Scripts\Activate.ps1

# Activate — Windows (Command Prompt):
.venv\Scripts\activate.bat

# Activate — macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
copy .env.example .env   # Windows
# or
cp .env.example .env     # macOS/Linux
```

Edit `.env` and fill in your credentials:

```
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
```

#### Getting a Gmail App Password

1. Go to your [Google Account](https://myaccount.google.com/) and select **Security**.
2. Under "How you sign in to Google," enable **2-Step Verification** if it isn't already on.
3. Search for **App passwords**, or go directly to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
4. Create a new app password (choose any app/device label you like).
5. Copy the 16-character password into `.env`. Spaces are optional.

#### Getting an Anthropic API key

1. Sign in at [console.anthropic.com](https://console.anthropic.com/).
2. Go to **API Keys** and create a new key.
3. Paste it into `.env`.

## Usage

```bash
python digest.py
```

The script prints progress to the terminal and sends the digest to `jgod4520@gmail.com` when complete. A typical run takes 1–2 minutes depending on how many Axios editions are checked.

## Project structure

```
policy-digest/
├── digest.py          # Main script
├── requirements.txt   # Python dependencies
├── .env.example       # Credentials template (safe to commit)
├── .env               # Your actual credentials (never committed)
├── .gitignore
└── README.md
```

## Debugging

Run with `--list` to print all fetched articles without filtering or sending email — useful for checking what the RSS feeds returned:

```cmd
python digest.py --list
```

## Cost estimate

Each run sends one Claude API request. With ~50–80 articles and their excerpts, expect roughly 8 000–12 000 input tokens and ~2 000 output tokens per run — well under $0.10 at current `claude-sonnet-4-6` pricing.
