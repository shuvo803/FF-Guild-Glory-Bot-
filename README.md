# # Free Fire Guild Glory Bot

A fully automatic **Guild Glory Bot** for Free Fire **India Server** with web interface, auto guest account generation, and real-time dashboard.

## Features

- **Web Interface** - Enter Guild ID and bot count, start/stop from browser
- **Guild Details** - Shows guild name, level, member count, capacity
- **Real Free Fire API** - Uses JWT + protobuf + AES encryption to interact with Free Fire servers
- **Guest Accounts** - Auto-generate or manually add UID/password pairs
- **Live Dashboard** - SSE real-time updates for all bot activity
- **PostgreSQL Storage** - Guest accounts stored in database (persistent)
- **Cross-platform** - Works on Render cloud, Termux (Android), or any PC

## One-Click Deploy on Render

1. Push this repo to GitHub
2. Go to https://dashboard.render.com
3. Click **New** → **Blueprint**
4. Connect your GitHub repo
5. Click **Apply**
6. Open `https://your-app.onrender.com`

## Manual Setup

```bash
pip install -r requirements.txt
python app.py
# Open http://localhost:5000-
