# 🍽️ FoodCheck — AI Food Safety Analyzer

AI-powered food product health and corporate transparency checker for the Indian market.

Upload a photo of any packaged food product, and FoodCheck will:
- **Extract** brand, product name, and ingredients using Google Gemini AI vision
- **Cross-check** against the Open Food Facts database
- **Analyze** health & safety with AI-powered scoring
- **Fetch** recent company news for transparency

## 🚀 Quick Start

### 1. Get API Key (Free)

**Gemini API Key (Required):**
1. Go to [Google AI Studio](https://aistudio.google.com/apikey)
2. Click "Create API Key"
3. Copy the key

**News Fetching:**
- **Primary (Default):** Google News India RSS Feed (Zero API key required, unlimited, bypasses college Wi-Fi blocks).
- **Secondary (Optional):** GNews.io API key (can be added if you have one).
- **Offline / Firewall Fallback:** Gemini AI Corporate Transparency engine.

### 2. Clone & Setup

```bash
git clone <your-repo-url>
cd foodcheck
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your API keys:

```
GEMINI_API_KEY=your_gemini_key_here
GNEWS_API_KEY=your_gnews_key_here
```

### 4. Run Locally

```bash
flask run
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## 🚂 Deploy to Railway

1. Push your code to a GitHub repository
2. Go to [Railway](https://railway.app) and create a new project
3. Connect your GitHub repo
4. Railway auto-detects the `Procfile` and deploys with gunicorn
5. Add environment variables in Railway dashboard:
   - `GEMINI_API_KEY`
   - `GNEWS_API_KEY`
6. Your app will be live at the generated Railway URL

## 📁 Project Structure

```
foodcheck/
├── app.py                  # Flask app entrypoint & routes
├── services/
│   ├── vision_extractor.py # Gemini AI vision — product label reading
│   ├── product_lookup.py   # Open Food Facts API lookup
│   ├── health_analyzer.py  # Gemini AI health/safety scoring
│   └── news_fetcher.py     # GNews API — company news
├── templates/
│   ├── index.html          # Upload form (drag & drop)
│   └── report.html         # Full product health report
├── static/
│   ├── style.css           # Dark theme UI styles
│   └── script.js           # Client-side upload & report logic
├── uploads/                # Temp storage (auto-cleaned)
├── .env.example            # Template for API keys
├── requirements.txt        # Python dependencies
├── Procfile                # Railway/gunicorn deployment
└── README.md               # This file
```

## 🔌 APIs Used

| Service | Purpose | Auth |
|---------|---------|------|
| [Google Gemini](https://aistudio.google.com) | Vision extraction & health analysis | API key (free tier) |
| [Open Food Facts](https://world.openfoodfacts.org) | Product/nutrition database | None required |
| [GNews](https://gnews.io) | Company news headlines | API key (free tier) |

## 📜 License

MIT
